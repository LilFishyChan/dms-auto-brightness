#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nabu-autobrightness
Intelligent Ambient Light Auto-Brightness Daemon for Xiaomi Pad 5 (nabu).

Features:
- Reads ambient light levels from net.hadess.SensorProxy (iio-sensor-proxy).
- Human perceptual curve (Stevens' power law / gamma response).
- Exponential Moving Average (EMA) filtering to eliminate hand-shadow jitter.
- Smooth fading transitions (cosine interpolation) for imperceptible steps.
- Native integration with KDE Plasma 6 PowerDevil (setBrightnessSilent).
- Automatic sysfs fallback (/sys/class/backlight/ktz8866-backlight).
- User manual brightness preference learning (bias offset).
- Unix domain socket IPC for instantaneous CLI control and desktop toggle.
- ScreenSaver lock and system suspend/resume power-saving hooks.

Copyright (c) 2026 Kumar-Jy <https://github.com/Kumar-Jy>
Licensed under the MIT License.
"""

import os
import sys
import math
import time
import json
import signal
import socket
import logging
import glob
import shutil
import argparse
import subprocess

try:
    import gi
    gi.require_version('Gio', '2.0')
    gi.require_version('GLib', '2.0')
    from gi.repository import Gio, GLib
except ImportError as e:
    sys.stderr.write(f"Error: PyGObject (python-gobject) is required: {e}\n")
    sys.exit(1)

VERSION = "1.0.0"
APP_NAME = "nabu-autobrightness"

DEFAULT_CONFIG = {
    "active": True,
    "profile": "standard",
    "user_bias": 0.0,
    # 两次"开始一次调整"之间的最小间隔（秒）。决定多久才重新评估一次
    "trigger_interval_s": 5,
    # 环境光变化小于这个百分比时不动屏幕（默认 10%，设置页 5-20）
    "lux_deadband_pct": 5,
    # 用来求平均的光照窗口（秒）：拿最近这段时间的均值做决策，不受瞬时抖动影响
    "lux_window_s": 5,
    # 用户手动调完亮度后，暂停自动调节多久（秒）
    "user_grace_s": 10,
    # 每次用户调整，偏好朝他的选择挪动多少（0-100%）。
    # 不做一次性覆盖：调一次只学一点，多次同向调整才会真正改变曲线。
    "learn_rate_pct": 10,
    "smooth_duration": 0.8,
    "deadband_pct": 1.2,
    "notify_on_toggle": True,
}

PROFILES = {
    "standard": {
        "description": "Ergonomic human-calibrated curve for indoor and outdoor comfort",
        "anchors": [
            (0.0, 8.0),     # Pitch dark (bedroom)
            (2.0, 12.0),    # Ultra-dim ambient
            (5.0, 18.0),    # Soft night light
            (15.0, 28.0),   # Dim evening room
            (40.0, 42.0),   # Cozy indoor living room
            (80.0, 55.0),   # Normal lit room
            (150.0, 66.0),  # Bright office / study room
            (300.0, 78.0),  # Bright workspace
            (600.0, 88.0),  # Daylight near window
            (1000.0, 100.0) # Full sun / bright outdoors
        ],
    },
    "bright": {
        "description": "Vibrant boost for well-lit rooms and outdoor visibility",
        "anchors": [
            (0.0, 12.0),
            (2.0, 18.0),
            (5.0, 26.0),
            (15.0, 38.0),
            (40.0, 54.0),
            (80.0, 68.0),
            (150.0, 80.0),
            (300.0, 90.0),
            (600.0, 100.0)
        ],
    },
    "dim": {
        "description": "Lower glare curve for night reading and dark environments",
        "anchors": [
            (0.0, 5.0),
            (2.0, 8.0),
            (5.0, 12.0),
            (15.0, 18.0),
            (40.0, 28.0),
            (80.0, 38.0),
            (150.0, 48.0),
            (300.0, 60.0),
            (600.0, 72.0),
            (1000.0, 85.0)
        ],
    },
    "battery": {
        "description": "Power-saving curve capped at 68% max brightness",
        "anchors": [
            (0.0, 5.0),
            (2.0, 7.0),
            (5.0, 10.0),
            (15.0, 16.0),
            (40.0, 25.0),
            (80.0, 35.0),
            (150.0, 45.0),
            (300.0, 55.0),
            (600.0, 68.0)
        ],
    },
}


def calculate_profile_brightness_pct(lux, profile_name="standard", bias=0.0):
    """Calculates target screen brightness percentage from lux using log-space cosine anchor interpolation."""
    cfg = PROFILES.get(profile_name, PROFILES["standard"])
    anchors = cfg["anchors"]
    l = max(0.0, float(lux))
    if l <= anchors[0][0]:
        base = anchors[0][1]
    elif l >= anchors[-1][0]:
        base = anchors[-1][1]
    else:
        base = anchors[-1][1]
        for i in range(len(anchors) - 1):
            x0, y0 = anchors[i]
            x1, y1 = anchors[i + 1]
            if x0 <= l <= x1:
                if x0 > 0:
                    t = (math.log(l) - math.log(x0)) / (math.log(x1) - math.log(x0))
                else:
                    t = (l - x0) / (x1 - x0)
                # Smooth cosine S-curve easing between anchors
                t_smooth = 0.5 * (1.0 - math.cos(math.pi * t))
                base = y0 + (y1 - y0) * t_smooth
                break

    min_allowed = anchors[0][1]
    max_allowed = anchors[-1][1] if profile_name in ["dim", "battery"] else 100.0
    return max(min_allowed, min(max_allowed, base + bias))



def get_runtime_dir():
    return os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")


def get_config_path():
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(config_home, APP_NAME, "config.json")


def get_socket_path():
    return os.path.join(get_runtime_dir(), f"{APP_NAME}.sock")


def get_state_path():
    return os.path.join(get_runtime_dir(), f"{APP_NAME}.state")


def get_probe_path():
    """Shell 侧（Daemon.qml）写出的真实亮度探针："<0-100> <unix时间戳>"。"""
    return os.path.join(get_runtime_dir(), APP_NAME, "brightness.txt")


def send_notification(title, message, icon="display-brightness-symbolic"):
    try:
        subprocess.run(
            ["notify-send", "-a", "Auto-Brightness", "-i", icon, title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


class Config:
    """配置存进 DMS 的 plugin_settings.json（pluginId = nabuAutoBrightness），
    这样插件设置页改完立即生效，也能跟着插件一起打包分发。
    独跑（没有 DMS）时退回 ~/.config/nabu-autobrightness/config.json。
    """

    DMS_PATH = os.path.expanduser("~/.config/dms/plugin_settings.json")
    NODE = "nabuAutoBrightness"

    def __init__(self):
        self.path = self._pick_path()
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def _pick_path(self):
        if os.path.isdir(os.path.dirname(self.DMS_PATH)):
            return self.DMS_PATH
        return get_config_path()

    def _is_dms(self):
        return self.path == self.DMS_PATH

    def load(self):
        if not os.path.exists(self.path):
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            loaded = raw.get(self.NODE, {}) if self._is_dms() else raw
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception as e:
            logging.warning(f"Failed to load config from {self.path}: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if self._is_dms():
                raw = {}
                if os.path.exists(self.path):
                    try:
                        with open(self.path, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                    except Exception:
                        raw = {}
                raw[self.NODE] = self.data
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(raw, f, indent=2)
            else:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save config to {self.path}: {e}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()


class BacklightBackend:
    """屏幕背光控制，后端按优先级自动探测：

    1. DMS IPC    dms ipc call brightness set <pct> [device]   ← DMS 桌面
    2. PowerDevil KDE DBus setBrightnessSilent                  ← KDE Plasma
    3. sysfs      /sys/class/backlight/*                        ← 通用兜底
    """

    def __init__(self, preferred_device=""):
        self.backend_type = "unknown"
        self.device = preferred_device        # DMS 设备 id 或 sysfs 路径
        self.max_brightness = 10000
        self._last_known = None
        self.session_bus = None
        self.powerdevil_proxy = None
        self._sysfs_path = None
        self._init_backend()

    # ── 探测 ───────────────────────────────────────────────
    def _init_backend(self):
        if self._try_dms():
            return
        if self._try_powerdevil():
            return
        if self._try_sysfs():
            return
        self.backend_type = "Unavailable"
        logging.error("No suitable backlight backend found!")

    def _try_dms(self):
        if not shutil.which("dms"):
            return False
        try:
            r = subprocess.run(
                ["dms", "ipc", "call", "brightness", "list"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode != 0:
                return False
            devs = []
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.endswith("(backlight)"):
                    devs.append(line.split()[0])
            if not devs:
                return False
            if self.device not in devs:
                self.device = devs[0]
            self.backend_type = f"DMS IPC ({self.device})"
            logging.info(f"Initialized backend: {self.backend_type}")
            return True
        except Exception as e:
            logging.debug(f"DMS backend not available: {e}")
            return False

    def _try_powerdevil(self):
        try:
            self.session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.powerdevil_proxy = Gio.DBusProxy.new_sync(
                self.session_bus,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.kde.Solid.PowerManagement",
                "/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
                "org.kde.Solid.PowerManagement.Actions.BrightnessControl",
                None,
            )
            res = self.powerdevil_proxy.call_sync(
                "brightnessMax", None, Gio.DBusCallFlags.NONE, 2000, None
            )
            self.max_brightness = res.unpack()[0]
            self.backend_type = "KDE PowerDevil (DBus)"
            logging.info(f"Initialized backend: {self.backend_type} (max: {self.max_brightness})")
            return True
        except Exception as e:
            logging.debug(f"PowerDevil DBus not available: {e}")
            self.powerdevil_proxy = None
            return False

    def _try_sysfs(self):
        for d in sorted(glob.glob("/sys/class/backlight/*")):
            maxp = os.path.join(d, "max_brightness")
            bpath = os.path.join(d, "brightness")
            if not (os.path.isdir(d) and os.path.exists(maxp) and os.path.exists(bpath)):
                continue
            if not os.access(bpath, os.W_OK):
                continue
            try:
                with open(maxp) as f:
                    self.max_brightness = int(f.read().strip())
            except Exception:
                continue
            self._sysfs_path = d
            self.backend_type = f"Sysfs ({os.path.basename(d)})"
            logging.info(f"Initialized backend: {self.backend_type} (max: {self.max_brightness})")
            return True
        return False

    def read_probe_history(self):
        """读 shell 侧写的亮度历史：[(0..10000 亮度, 时间戳), ...]，旧→新。

        daemon 是 DMS 的子进程，`dms ipc call brightness status` 在这里会超时
        （读回来的一直是兜底值），所以亮度只能由 shell 侧提供。
        """
        out = []
        try:
            with open(get_probe_path()) as f:
                for ln in f.read().strip().splitlines()[-20:]:
                    parts = ln.split()
                    if len(parts) >= 2:
                        out.append((int(float(parts[0])) * 100, float(parts[1])))
        except Exception:
            pass
        return out

    def read_probe(self):
        """当前亮度（历史里的最后一条）。"""
        hist = self.read_probe_history()
        return hist[-1] if hist else (None, None)

    # ── 读 ─────────────────────────────────────────────────
    def get_brightness(self):
        """返回 0..10000 归一化亮度。

        优先读 shell 侧探针：daemon 是 DMS 的子进程，在这里调
        `dms ipc call brightness status` 会超时并返回兜底值，整个闭环就
        建立在错误读数上了。
        """
        probe_val, _ = self.read_probe()
        if probe_val is not None:
            self._last_known = probe_val
            return probe_val

        if self.backend_type.startswith("DMS"):
            try:
                r = subprocess.run(
                    ["dms", "ipc", "call", "brightness", "status"],
                    capture_output=True, text=True, timeout=10,
                )
                m = re.search(r"Brightness:\s*(\d+)%", r.stdout)
                if m:
                    self._last_known = int(m.group(1)) * 100
                    return self._last_known
                logging.debug(f"DMS brightness status unparsable: {r.stdout!r} / {r.stderr!r}")
            except Exception as e:
                logging.debug(f"DMS get brightness error: {e}")
            # 读失败沿用上次已知值——返回一个假值会让整个闭环建立在错误读数上
            return self._last_known if self._last_known is not None else 5000

        if self.powerdevil_proxy:
            try:
                res = self.powerdevil_proxy.call_sync(
                    "brightness", None, Gio.DBusCallFlags.NONE, 1000, None
                )
                raw = res.unpack()[0]
                if self.max_brightness != 10000 and self.max_brightness > 0:
                    return int(round((raw / self.max_brightness) * 10000))
                return raw
            except Exception as e:
                logging.debug(f"DBus get brightness error: {e}")

        if self._sysfs_path:
            try:
                with open(os.path.join(self._sysfs_path, "brightness")) as f:
                    raw = int(f.read().strip())
                return int(round((raw / max(1, self.max_brightness)) * 10000))
            except Exception as e:
                logging.debug(f"Sysfs get brightness error: {e}")
        return 5000

    # ── 写 ─────────────────────────────────────────────────
    def set_brightness_silent(self, value):
        """设置亮度（0..10000 归一化）。走 DMS IPC 时由 DMS 决定是否弹 OSD。"""
        val = max(0, min(10000, int(value)))

        if self.backend_type.startswith("DMS"):
            pct = max(1, min(100, int(round(val / 100.0))))
            cmd = ["dms", "ipc", "call", "brightness", "set", str(pct)]
            if self.device:
                cmd.append(self.device)
            try:
                subprocess.run(cmd, capture_output=True, timeout=3)
                return True
            except Exception as e:
                logging.debug(f"DMS set brightness error: {e}")

        if self.powerdevil_proxy:
            try:
                target = (int(round((val / 10000.0) * self.max_brightness))
                          if self.max_brightness != 10000 else val)
                self.powerdevil_proxy.call_sync(
                    "setBrightnessSilent",
                    GLib.Variant("(i)", (target,)),
                    Gio.DBusCallFlags.NONE, 1000, None,
                )
                return True
            except Exception as e:
                logging.debug(f"DBus set brightness error: {e}")

        if self._sysfs_path:
            try:
                raw_target = int(round((val / 10000.0) * self.max_brightness))
                with open(os.path.join(self._sysfs_path, "brightness"), "w") as f:
                    f.write(f"{raw_target}\n")
                return True
            except Exception as e:
                logging.debug(f"Sysfs write brightness error: {e}")
        return False


class AutoBrightnessDaemon:
    """Core daemon managing light sensor, curve calculations, smoothing, and transitions."""

    def __init__(self):
        self.config = Config()
        self.backlight = BacklightBackend()

        self.system_bus = None
        self.sensor_proxy = None

        self.raw_lux = 10.0
        self.smoothed_lux = 10.0
        self.current_brightness = self.backlight.get_brightness()
        self.target_brightness = self.current_brightness

        self.screen_locked = False
        self.system_suspended = False
        self.light_claimed = False

        # Slew transition follower state
        self.current_val = float(self.current_brightness)
        self.fader_timer_id = None
        self.is_fading = False
        self.ignore_brightness_until = 0.0
        # 用户正在手动调亮度的截止时间；在此之前绝不写亮度
        self.user_adjust_until = 0.0
        self.resume_after_adjust_id = None
        # 最近 1.5 秒内我们自己写过的值，用来区分"回显"和"用户在拖"
        self.recent_writes = []
        # 上次真正调整屏幕时的环境光，用于光照死区
        self.last_applied_lux = None
        # 最近 lux_window_s 内的采样，用均值做决策
        self.lux_samples = []
        self.last_trigger_at = 0.0
        self.last_target_dir = 0
        # 上次真正写进 DMS 的整数百分比（DMS 只接受整数）
        self.last_written_pct = None
        # 最近若干次"要不要调整"的决策，写进 state 方便排查抖动
        self.decisions = []
        # 已经处理过的用户调整时间戳：同一条历史不能反复触发让路
        self.last_seen_user_ts = 0.0
        # 本次调整开始时锁定的光照：slew 过程中目标不能跟着窗口漂，
        # 否则步进方向来回翻转，屏幕会一高一低
        self.slew_target_lux = None
        # 诊断：让路次数与最近一次让路的现场，写进 state 文件方便排查
        self.yield_count = 0
        self.last_yield = None

        # IPC Socket
        self.server_sock = None
        self.sock_watch_id = None

        # Periodic sensor heartbeat
        self.heartbeat_id = None

    def start(self):
        logging.info(f"Starting {APP_NAME} v{VERSION}...")
        self._init_system_bus()
        self._init_session_bus()
        self._init_ipc_socket()
        self._claim_sensor()

        # Write initial state
        self._update_state_file()

        # Periodic heartbeat (every 2.0s) to keep sensor alive and check status
        self.heartbeat_id = GLib.timeout_add_seconds(2, self._on_heartbeat)

        # Handle signals
        try:
            from gi.repository import GLibUnix
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_signal_exit)
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_signal_exit)
        except Exception:
            signal.signal(signal.SIGTERM, lambda s, f: self._on_signal_exit())
            signal.signal(signal.SIGINT, lambda s, f: self._on_signal_exit())

        logging.info(f"Daemon active and running. Backend: {self.backlight.backend_type}")

    def _init_system_bus(self):
        try:
            self.system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self.sensor_proxy = Gio.DBusProxy.new_sync(
                self.system_bus,
                Gio.DBusProxyFlags.NONE,
                None,
                "net.hadess.SensorProxy",
                "/net/hadess/SensorProxy",
                "net.hadess.SensorProxy",
                None,
            )
            self.sensor_proxy.connect("g-properties-changed", self._on_sensor_properties_changed)
            self.sensor_proxy.connect("notify::g-name-owner", self._on_sensor_name_owner_changed)

            # Subscribe to system sleep/resume
            self.system_bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
                None,
            )
        except Exception as e:
            logging.error(f"Failed to connect to system bus / SensorProxy: {e}")

    def _init_session_bus(self):
        try:
            session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            # Subscribe to PowerDevil brightnessChanged to detect user manual slider moves
            session_bus.signal_subscribe(
                "org.kde.Solid.PowerManagement",
                "org.kde.Solid.PowerManagement.Actions.BrightnessControl",
                "brightnessChanged",
                "/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_brightness_changed,
                None,
            )

            # Subscribe to ScreenSaver ActiveChanged
            session_bus.signal_subscribe(
                "org.freedesktop.ScreenSaver",
                "org.freedesktop.ScreenSaver",
                "ActiveChanged",
                "/ScreenSaver",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_screensaver_changed,
                None,
            )
        except Exception as e:
            logging.debug(f"Session bus subscriptions failed: {e}")

    def _on_sensor_name_owner_changed(self, proxy, pspec):
        owner = proxy.get_name_owner()
        if owner:
            logging.info(f"SensorProxy appeared on D-Bus ({owner}), claiming light sensor...")
            self.light_claimed = False
            self._claim_sensor()
        else:
            logging.info("SensorProxy disappeared from D-Bus, resetting light claimed state.")
            self.light_claimed = False

    def _claim_sensor(self):
        if not self.config.get("active", True):
            logging.info("Auto-brightness disabled in config, skipping ClaimLight.")
            return

        if self.sensor_proxy and not self.light_claimed:
            if not self.sensor_proxy.get_name_owner():
                return
            try:
                self.sensor_proxy.call_sync("ClaimLight", None, Gio.DBusCallFlags.NONE, 2000, None)
                self.light_claimed = True
                logging.info("Claimed ambient light sensor successfully.")

                # Read initial cached lux
                prop = self.sensor_proxy.get_cached_property("LightLevel")
                if prop:
                    self._on_ambient_light_update(float(prop.get_double()), force=True)
            except Exception as e:
                logging.warning(f"Could not claim light sensor: {e}")

    def _release_sensor(self):
        if self.sensor_proxy and self.light_claimed:
            try:
                self.sensor_proxy.call_sync("ReleaseLight", None, Gio.DBusCallFlags.NONE, 1000, None)
                self.light_claimed = False
                logging.info("Released ambient light sensor.")
            except Exception as e:
                logging.debug(f"Error releasing light sensor: {e}")

    def _on_sensor_properties_changed(self, proxy, changed_props, invalidated_props):
        props = changed_props.unpack()
        if "LightLevel" in props:
            lux = float(props["LightLevel"])
            self._on_ambient_light_update(lux)

    def _on_heartbeat(self):
        """Periodic safety check, config hot-reload and sensor polling."""
        # 热重载：DMS 设置页改 profile / bias 后立即生效
        try:
            prev_profile = self.config.get("profile", "standard")
            self.config.load()
            if self.config.get("profile", "standard") != prev_profile:
                logging.info(f"Profile hot-reloaded -> {self.config.get('profile')}")
                self._evaluate_brightness(force=True)
        except Exception:
            pass
        if not self.config.get("active", True) or self.screen_locked or self.system_suspended:
            return True

        if self.sensor_proxy and self.sensor_proxy.get_name_owner():
            if not self.light_claimed:
                self._claim_sensor()

            prop = self.sensor_proxy.get_cached_property("LightLevel")
            if prop:
                lux = float(prop.get_double())
                if abs(lux - self.raw_lux) > 0.1:
                    self._on_ambient_light_update(lux)

        self._update_state_file()
        return True

    def _on_prepare_for_sleep(self, connection, sender, path, interface, signal_name, parameters, user_data):
        is_sleeping = parameters.unpack()[0]
        if is_sleeping:
            logging.info("System preparing for sleep - pausing autobrightness")
            self.system_suspended = True
            self._stop_fade()
            self._release_sensor()
        else:
            logging.info("System resumed from sleep - restoring autobrightness")
            self.system_suspended = False
            # Wait 1.5s for sensors to reinitialize
            GLib.timeout_add(1500, self._resume_after_wake)

    def _resume_after_wake(self):
        self._claim_sensor()
        self.current_brightness = self.backlight.get_brightness()
        self._evaluate_brightness(force=True)
        return False

    def _on_screensaver_changed(self, connection, sender, path, interface, signal_name, parameters, user_data):
        is_active = parameters.unpack()[0]
        self.screen_locked = bool(is_active)
        logging.info(f"Screen lock state changed: {self.screen_locked}")
        if not self.screen_locked:
            # Re-evaluate upon unlock
            GLib.timeout_add(300, self._evaluate_after_unlock)

    def _evaluate_after_unlock(self):
        self.current_brightness = self.backlight.get_brightness()
        self._evaluate_brightness()
        return False

    def _on_brightness_changed(self, connection, sender, path, interface, signal_name, parameters, user_data):
        new_val = parameters.unpack()[0]

        # 自己刚写下去的值，回显忽略
        if time.time() < self.ignore_brightness_until:
            return

        # 和"我们正打算写/刚写的值"差得远 → 这是用户在拖滑块，
        # 不是我们的 slew 回显。即使是 is_fading 期间也要认出来，
        # 否则会一边被用户拖、一边被 slew 拉回去（用户说的"抢亮度"）。
        if abs(new_val - self.current_val) <= 250:
            return

        self._yield_to_user(new_val)

    # 自己写过什么，保留够长以覆盖整个 slew 过程；容差 1.5%
    OWN_WRITE_WINDOW_S = 30.0
    OWN_WRITE_TOLERANCE = 150

    def _note_write(self, val):
        now = time.time()
        self.recent_writes.append((now, val))
        self.recent_writes = [(t, v) for t, v in self.recent_writes
                              if now - t < self.OWN_WRITE_WINDOW_S]

    def _is_own_write(self, val):
        """这个亮度值是不是我们自己写过的。

        容差要小：±4% 时用户拖几格正好落进我们刚写过的值附近，就被判成
        "自己写的"而忽略掉，结果继续被覆盖（用户看到的左右跳）。
        """
        now = time.time()
        return any(abs(val - v) <= self.OWN_WRITE_TOLERANCE and now - t < self.OWN_WRITE_WINDOW_S
                   for t, v in self.recent_writes)

    def _user_moved_recently(self, window=4.0):
        """在探针历史里找出**新出现的**、不是我们写的那条变化。

        必须按时间戳去重：否则用户调过一次之后，那条记录会在窗口里反复被
        认成"又动了"，导致让路—恢复—再让路，屏幕看起来一高一低。
        """
        now = time.time()
        for val, ts in reversed(self.backlight.read_probe_history()):
            if now - ts > window:
                break
            if ts <= self.last_seen_user_ts:
                break
            if not self._is_own_write(val):
                self.last_seen_user_ts = ts
                return val
        return None

    def _yield_to_user(self, val):
        """用户在手动调亮度：停掉自动调节，让他先调，停手后再接管。"""
        self.yield_count += 1
        self.last_yield = {
            "at": round(time.time(), 1),
            "val": int(val),
            "current_val": int(self.current_val),
            "lux": round(float(self.smoothed_lux or 0.0), 1),
        }
        self._stop_fade()
        self.slew_target_lux = None
        grace = float(self.config.get("user_grace_s", self.DEFAULT_USER_GRACE_S))
        self.user_adjust_until = time.time() + grace
        self._arm_resume_after_adjust()

        if self.config.get("active", True):
            lux_now = self._avg_lux()
            # 用户想要的偏移（百分点）：他选的亮度 - 当前曲线给的值
            curve_now = calculate_profile_brightness_pct(
                lux_now, self.config.get("profile", "standard"), bias=0.0)
            desired = max(-35.0, min(35.0, val / 100.0 - curve_now))
            learned = float(self.config.get("user_bias", 0.0))
            rate = max(0.0, min(1.0, float(self.config.get("learn_rate_pct", 30)) / 100.0))
            updated = learned + (desired - learned) * rate
            self.config.set("user_bias", round(updated, 2))
            self.last_applied_lux = float(lux_now)
            logging.info(
                f"User manual slider change: {val} at {lux_now:.1f} lux "
                f"(want {desired:+.1f}%, learned {learned:+.1f}% -> {updated:+.1f}%)"
            )

        self.target_brightness = val
        self.current_val = float(val)
        self.current_brightness = val
        self.ignore_brightness_until = time.time() + 1.0
        self._update_state_file()

    def _arm_resume_after_adjust(self):
        """用户停手一段时间后再恢复自动调节。"""
        if self.resume_after_adjust_id is not None:
            GLib.source_remove(self.resume_after_adjust_id)
        self.resume_after_adjust_id = GLib.timeout_add(500, self._resume_after_adjust)

    def _resume_after_adjust(self):
        if time.time() < self.user_adjust_until:
            return True                     # 还在调整窗口内，继续等
        self.resume_after_adjust_id = None
        # 以用户最终设定的亮度为起点继续收敛
        self.current_brightness = self.backlight.get_brightness()
        self.current_val = float(self.current_brightness)
        self.ignore_brightness_until = time.time() + 0.5
        logging.info(f"User adjustment finished, resuming auto-brightness at {self.current_brightness}")
        self._start_slew_transition()
        return False

    def _push_lux(self, lux):
        """把采样放进滑动窗口，窗口长度由 lux_window_s 决定。"""
        now = time.time()
        window = float(self.config.get("lux_window_s", self.DEFAULT_LUX_WINDOW_S))
        self.lux_samples.append((now, float(lux)))
        self.lux_samples = [(t, v) for t, v in self.lux_samples if now - t <= window]
        if len(self.lux_samples) > 200:
            self.lux_samples = self.lux_samples[-200:]

    def _avg_lux(self):
        """最近一个窗口内的平均光照；没有采样就退回瞬时值。"""
        if not self.lux_samples:
            return self.raw_lux
        return sum(v for _, v in self.lux_samples) / len(self.lux_samples)

    def _on_ambient_light_update(self, lux, force=False):
        self.raw_lux = max(0.0, float(lux))
        self._push_lux(self.raw_lux)
        if self.smoothed_lux is None or force:
            self.smoothed_lux = self.raw_lux
        self._start_slew_transition()

    def _evaluate_brightness(self, force=False):
        if force:
            self.smoothed_lux = self.raw_lux
        self._start_slew_transition(force=force)

    # 一次调整内部按小步渐变（平滑，不突然跳）；多久才"开始下一次调整"
    # 由 trigger_interval_s 控制，两者是两个概念。
    SLEW_TICK_MS = 200
    DEFAULT_USER_GRACE_S = 10
    DEFAULT_TRIGGER_INTERVAL_S = 5
    DEFAULT_LUX_DEADBAND_PCT = 5
    DEFAULT_LUX_WINDOW_S = 5
    # 差距小于这个（2%）就不要再微调了，直接落到目标
    SETTLE_THRESHOLD = 100
    # 目标亮度和当前亮度差不到这个（3%）就根本不启动调整。
    # 室内环境光天然在 ±10% 抖，过了 lux 死区但换算到亮度只差 1-2%，
    # 每次挪一点就是用户看到的"左右跳"。
    MIN_BRIGHTNESS_DELTA = 800
    # 单次调整的幅度上限。常规调整小步走；明显的光照突变（开灯/关灯）
    # 走快速通道，一次性挪到位。上限本身是为了压制正反馈引起的来回摆。
    MAX_STEP_PER_ADJUST = 10000
    MAX_STEP_ON_BIG_CHANGE = 10000

    def _log_decision(self, action, avg, target_now, extra=""):
        self.decisions = (self.decisions + [{
            "at": round(time.time(), 1),
            "action": action,
            "avg_lux": round(float(avg), 1),
            "target": int(target_now),
            "current": int(self.current_brightness),
            "delta": int(target_now - self.current_brightness),
            "extra": extra,
        }])[-12:]

    def _start_slew_transition(self, force=False):
        if self.fader_timer_id is not None:
            return

        now = time.time()

        avg = self._avg_lux()

        if force:
            # 用户主动改了设定（profile 等）：立即生效，不看间隔和死区
            self._log_decision("adjust_forced", avg, self.current_brightness)
            self.last_trigger_at = now
            self.slew_target_lux = avg
            self.is_fading = True
            self.fader_timer_id = GLib.timeout_add(self.SLEW_TICK_MS, self._on_fader_tick)
            return

        # 判断是不是"明显的光照突变"（开灯/关灯这种级别）
        big_change = False
        if self.last_applied_lux is not None and self.last_applied_lux > 1.0:
            big_change = (avg > self.last_applied_lux * 2.0) or (avg < self.last_applied_lux * 0.5)

        # 用户刚手动调过亮度：这段时间内不许走快速通道。
        # 否则他刚设好的值会被一次 15% 的跳跃顶掉（"暗着突然变亮"）。
        if big_change and time.time() < self.user_adjust_until + grace:
            big_change = False

        # 两次常规调整之间至少隔 trigger_interval_s；突变不等
        trigger_s = float(self.config.get("trigger_interval_s", self.DEFAULT_TRIGGER_INTERVAL_S))
        if not big_change and self.last_trigger_at and now - self.last_trigger_at < trigger_s:
            self._log_decision("skip_interval", self._avg_lux(), self.current_brightness)
            return

        # 光照死区：拿"窗口均值"比，避免瞬时抖动触发
        avg = self._avg_lux()
        deadband = float(self.config.get("lux_deadband_pct", self.DEFAULT_LUX_DEADBAND_PCT)) / 100.0
        if deadband > 0 and self.last_applied_lux is not None and self.last_applied_lux > 0.5:
            ratio = abs(avg - self.last_applied_lux) / max(1.0, self.last_applied_lux)
            if ratio < deadband:
                self._log_decision("skip_lux_deadband", avg, self.current_brightness)
                logging.debug(
                    f"Lux deadband: {self.last_applied_lux:.1f} -> {avg:.1f} "
                    f"({ratio * 100:.0f}% < {deadband * 100:.0f}%), keep {self.current_brightness}"
                )
                return

        # 绝对曲线：关灯就一定要变暗，靠"增量"是回不到正确水平的。
        # 正反馈交给下面两道闸挡住——死区（小幅不动）和单次幅度上限（大步分几次走）。
        profile_now = self.config.get("profile", "standard")
        bias_now = float(self.config.get("user_bias", 0.0))
        # 目标直接量化到整数百分比：DMS 只能写整 %，目标落在 24.5% 这种位置时
        # 会在 24 和 25 之间来回跳（用户看到的"最后抖几下"）
        pct_target = int(round(calculate_profile_brightness_pct(avg, profile_now, bias=bias_now)))
        target_now = max(1, min(100, pct_target)) * 100
        # 滞回：朝同一方向的调整，差 5% 就够；反向回头要求更大（1.5×），
        # 否则噪声在阈值附近来回穿，屏幕就会左右跳
        delta = target_now - self.current_brightness
        same_dir = (delta > 0) == (self.last_target_dir >= 0)
        threshold = self.MIN_BRIGHTNESS_DELTA if same_dir else int(self.MIN_BRIGHTNESS_DELTA * 1.5)
        if abs(delta) < threshold:
            self._log_decision("skip_brightness_deadband", avg, target_now)
            logging.debug(
                f"Brightness delta too small: {self.current_brightness} -> {target_now} "
                f"({delta:+d}, need {threshold}), skip"
            )
            return
        self.last_target_dir = delta

        # 单次挪动幅度：常规小步（压正反馈），突变时一次到位
        cap = self.MAX_STEP_ON_BIG_CHANGE if big_change else self.MAX_STEP_PER_ADJUST
        target_now = max(self.current_brightness - cap,
                         min(self.current_brightness + cap, target_now))

        self._log_decision("adjust_big" if big_change else "adjust", avg, target_now)
        self.last_trigger_at = now
        self.slew_target_lux = avg
        self.is_fading = True
        self.fader_timer_id = GLib.timeout_add(self.SLEW_TICK_MS, self._on_fader_tick)

    def _on_fader_tick(self):
        # 每帧把最新的瞬时值也计入窗口，窗口自然滑动
        self._push_lux(self.raw_lux)
        # 用户正在调亮度：一帧都不要写
        if time.time() < self.user_adjust_until:
            self.is_fading = False
            self.fader_timer_id = None
            return False

        # 发现"用户正在拖"：在 shell 侧记录的亮度历史里，找一条不是我们写的值。
        # 不能只比"当前值"——daemon 写完探针就变成自己的值了，永远比不出来。
        moved = self._user_moved_recently(30.0)
        if moved is not None and abs(moved - self.current_val) > 200:
            self._yield_to_user(moved)
            self.is_fading = False
            self.fader_timer_id = None
            return False

        if not self.config.get("active", True) or self.screen_locked or self.system_suspended:
            self.is_fading = False
            self.fader_timer_id = None
            return False

        # 1. Continuous smooth convergence of smoothed_lux towards raw_lux
        lux_diff = self.raw_lux - self.smoothed_lux
        if abs(lux_diff) > 0.05:
            alpha = 0.15 if lux_diff > 0 else 0.06
            self.smoothed_lux += alpha * lux_diff
        else:
            self.smoothed_lux = self.raw_lux

        # 2. Re-calculate target brightness from continuously smoothed lux
        profile_name = self.config.get("profile", "standard")

        # 用本次调整开始时锁定的均值；slew 途中不再重新取平均，
        # 否则目标会一直漂，步进来回翻就成了"一高一低"
        avg_lux = self.slew_target_lux if self.slew_target_lux is not None else self._avg_lux()
        base_pct = calculate_profile_brightness_pct(avg_lux, profile_name, bias=0.0)

        # 曲线 + 多次学习累积出来的偏好偏移
        bias = float(self.config.get("user_bias", 0.0))
        final_pct = max(0.0, min(100.0, base_pct + bias))

        # 目标必须是整数百分比：否则 48.95% 这种值会让它在 48/49 之间来回写
        self.target_brightness = max(1, min(100, int(round(final_pct)))) * 100

        # 3. Slew screen brightness towards target_brightness
        diff = self.target_brightness - self.current_val
        lux_converged = abs(self.raw_lux - self.smoothed_lux) <= 0.05

        # 只要亮度到位就收敛。之前还要求 lux_converged（平滑光照与瞬时的差
        # ≤0.05），传感器一直在小幅跳动，这个条件几乎永远不成立，于是 tick
        # 停不下来、反复写 37/38
        if abs(diff) <= self.SETTLE_THRESHOLD:
            settle_pct = max(1, min(100, int(round(self.target_brightness / 100.0))))
            self.current_val = float(settle_pct * 100)
            self.current_brightness = settle_pct * 100
            self.backlight.set_brightness_silent(settle_pct * 100)
            self._note_write(self.target_brightness)
            self.last_written_pct = max(1, min(100, int(round(self.target_brightness / 100.0))))
            self.last_applied_lux = avg_lux
            self.slew_target_lux = None
            self.is_fading = False
            self.fader_timer_id = None
            self.ignore_brightness_until = time.time() + 0.5
            self._update_state_file()
            return False

        # 平滑渐变：每帧走差额的两成，300ms 一帧约 1.5 秒到位
        # 最小步长必须小于收敛阈值（1%），否则每次都跨过目标原地来回跳
        # 每帧走剩余差额的 55%：200ms 一帧，5 帧（约 1 秒）内基本到位
        if diff > 0:
            step = max(100.0, diff * 0.55)
            self.current_val += step
        else:
            step = max(100.0, abs(diff) * 0.55)
            self.current_val -= step

        val_int = int(round(self.current_val))
        pct = max(1, min(100, int(round(val_int / 100.0))))
        # DMS 只认整数百分比：24.5% 会被写成 24 或 25，来回就是 1% 的抖动。
        # 同一个百分比不重复写。
        if pct == self.last_written_pct:
            # 关键：内部值必须一起对齐到真实写下去的整数百分比，
            # 否则 current_val 会一直漂，下一帧又算出另一个百分比 → 30↔31↔32 来回抖
            self.current_val = float(pct * 100)
            self.current_brightness = pct * 100
            if abs(self.target_brightness - self.current_brightness) <= self.SETTLE_THRESHOLD:
                self.is_fading = False
                self.fader_timer_id = None
                self.ignore_brightness_until = time.time() + 0.5
                self.last_applied_lux = self.slew_target_lux
                self.slew_target_lux = None
                return False
            return True
        self.last_written_pct = pct
        self.backlight.set_brightness_silent(val_int)
        self._note_write(pct * 100)
        self.current_brightness = pct * 100
        self.ignore_brightness_until = time.time() + 0.5
        self._update_state_file()
        return True

    def _stop_fade(self):
        if self.fader_timer_id is not None:
            GLib.source_remove(self.fader_timer_id)
            self.fader_timer_id = None
        self.is_fading = False

    def _init_ipc_socket(self):
        sock_path = get_socket_path()
        if os.path.exists(sock_path):
            try:
                os.remove(sock_path)
            except Exception:
                pass

        try:
            self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_sock.bind(sock_path)
            self.server_sock.listen(5)
            self.server_sock.setblocking(False)

            self.sock_watch_id = GLib.io_add_watch(
                self.server_sock.fileno(), GLib.IOCondition.IN, self._on_socket_connection
            )
            logging.info(f"Listening on IPC socket: {sock_path}")
        except Exception as e:
            logging.error(f"Failed to initialize IPC socket {sock_path}: {e}")

    def _on_socket_connection(self, fd, condition):
        try:
            conn, _ = self.server_sock.accept()
            conn.settimeout(1.0)
            data = conn.recv(2048)
            if data:
                req = json.loads(data.decode("utf-8"))
                cmd = req.get("command", "")
                resp = self._handle_ipc_command(cmd, req)
                conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
            conn.close()
        except Exception as e:
            logging.debug(f"IPC connection handling error: {e}")
        return True

    def _handle_ipc_command(self, cmd, req):
        if cmd == "status":
            return {
                "success": True,
                "version": VERSION,
                "active": self.config.get("active", True),
                "profile": self.config.get("profile", "standard"),
                "user_bias": self.config.get("user_bias", 0.0),
                "raw_lux": round(self.raw_lux, 1),
                "smoothed_lux": round(self.smoothed_lux, 1),
                "current_brightness": self.current_brightness,
                "target_brightness": self.target_brightness,
                "backend": self.backlight.backend_type,
                "user_adjust_until": round(self.user_adjust_until, 2),
                "trigger_interval_s": self.config.get("trigger_interval_s", self.DEFAULT_TRIGGER_INTERVAL_S),
                "user_grace_s": self.config.get("user_grace_s", self.DEFAULT_USER_GRACE_S),
                "lux_deadband_pct": self.config.get("lux_deadband_pct", self.DEFAULT_LUX_DEADBAND_PCT),
                "avg_lux": round(self._avg_lux(), 1),
                "screen_locked": self.screen_locked,
                "light_claimed": self.light_claimed,
            }

        elif cmd == "toggle":
            new_state = not self.config.get("active", True)
            self.config.set("active", new_state)
            if new_state:
                self._claim_sensor()
                self._evaluate_brightness(force=True)
            else:
                self._stop_fade()
                self._release_sensor()

            self._update_state_file()

            if self.config.get("notify_on_toggle", True):
                if new_state:
                    send_notification(
                        "Auto-Brightness Enabled",
                        f"Ambient: {self.smoothed_lux:.0f} lx → Brightness: {self.target_brightness // 100}%",
                    )
                else:
                    send_notification(
                        "Auto-Brightness Disabled",
                        f"Screen locked at {self.current_brightness // 100}%",
                        icon="display-brightness-low-symbolic",
                    )

            return {"success": True, "active": new_state}

        elif cmd == "enable":
            self.config.set("active", True)
            self._claim_sensor()
            self._evaluate_brightness(force=True)
            self._update_state_file()
            return {"success": True, "active": True}

        elif cmd == "disable":
            self.config.set("active", False)
            self._stop_fade()
            self._release_sensor()
            self._update_state_file()
            return {"success": True, "active": False}

        elif cmd == "set_profile":
            p = req.get("profile", "")
            if p in PROFILES:
                self.config.set("profile", p)
                self._evaluate_brightness(force=True)
                self._update_state_file()
                if self.config.get("notify_on_toggle", True):
                    send_notification(
                        "Auto-Brightness Profile",
                        f"Active profile changed to '{p.capitalize()}'",
                    )
                return {"success": True, "profile": p}
            return {"success": False, "error": f"Invalid profile '{p}'"}

        elif cmd == "set_bias":
            bias = float(req.get("bias", 0.0))
            bias = max(-35.0, min(35.0, bias))
            self.config.set("user_bias", bias)
            self._evaluate_brightness(force=True)
            self._update_state_file()
            return {"success": True, "user_bias": bias}

        elif cmd == "reset_bias":
            self.config.set("user_bias", 0.0)
            # 清掉锚点 → 回到纯曲线
            self._evaluate_brightness(force=True)
            self._update_state_file()
            return {"success": True, "user_bias": 0.0}

        return {"success": False, "error": f"Unknown command '{cmd}'"}

    def _update_state_file(self):
        state_path = get_state_path()
        tmp_path = f"{state_path}.tmp"
        try:
            state = {
                "version": VERSION,
                "updated_at": time.time(),
                "active": self.config.get("active", True),
                "profile": self.config.get("profile", "standard"),
                "user_bias": self.config.get("user_bias", 0.0),
                "raw_lux": round(self.raw_lux, 1),
                "smoothed_lux": round(self.smoothed_lux, 1),
                "current_brightness": self.current_brightness,
                "target_brightness": self.target_brightness,
                "backend": self.backlight.backend_type,
                "yield_count": self.yield_count,
                "decisions": self.decisions,
                "last_yield": self.last_yield,
                "last_applied_lux": self.last_applied_lux,
                "slew_running": self.fader_timer_id is not None,
            }
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp_path, state_path)
        except Exception:
            pass

    def _on_signal_exit(self):
        logging.info("Received termination signal, exiting...")
        self._stop_fade()
        self._release_sensor()
        sock_path = get_socket_path()
        if os.path.exists(sock_path):
            try:
                os.remove(sock_path)
            except Exception:
                pass
        sys.exit(0)


# ============================================================================
# CLI Client Functions
# ============================================================================

def send_ipc_command(cmd, **kwargs):
    sock_path = get_socket_path()
    if not os.path.exists(sock_path):
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(sock_path)
        payload = {"command": cmd}
        payload.update(kwargs)
        s.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        data = s.recv(4096)
        s.close()
        if data:
            return json.loads(data.decode("utf-8"))
    except Exception:
        pass
    return None


def cli_status():
    status = send_ipc_command("status")
    if status is None:
        # Check if state file exists
        state_path = get_state_path()
        state = None
        if os.path.exists(state_path):
            try:
                with open(state_path, "r") as f:
                    state = json.load(f)
            except Exception:
                pass

        print("\033[1mAuto-Brightness\033[0m")
        print("----------------------------------------")
        print("  Daemon Status:       \033[31mInactive (stopped)\033[0m")
        if state:
            print(f"  Last Known Mode:     {'Enabled' if state.get('active') else 'Disabled'}")
            print(f"  Last Ambient Light:  {state.get('raw_lux', 0):.1f} lux")
            print(f"  Last Brightness:     {state.get('current_brightness', 0) // 100}%")
        print("\nTo start daemon:")
        print("  systemctl --user start nabu-autobrightness")
        return

    en = status.get("active", False)
    en_str = "\033[32mEnabled\033[0m" if en else "\033[31mDisabled\033[0m"
    bias = status.get("user_bias", 0.0)
    bias_str = f"{bias:+.1f}%" if bias != 0 else "0.0% (none)"

    print("\033[1mAuto-Brightness\033[0m")
    print("----------------------------------------")
    print(f"  Daemon Status:       \033[32mActive (running)\033[0m (v{status.get('version')})")
    print(f"  Auto-Brightness:     {en_str}")
    print(f"  Ambient Light:       {status.get('raw_lux', 0.0):.1f} lux (Smoothed: {status.get('smoothed_lux', 0.0):.1f} lux)")
    print(f"  Screen Brightness:   {status.get('current_brightness', 0) // 100}% ({status.get('current_brightness', 0)} / 10000)")
    print(f"  Target Brightness:   {status.get('target_brightness', 0) // 100}%")
    print(f"  Active Profile:      \033[36m{status.get('profile', 'standard')}\033[0m")
    print(f"  User Bias Offset:    {bias_str}")
    print(f"  Sensor State:        {'Claimed' if status.get('light_claimed') else 'Idle'}")
    print(f"  Backlight Backend:   {status.get('backend')}")
    print("----------------------------------------")


def cli_toggle():
    res = send_ipc_command("toggle")
    if res is None:
        # If daemon not running, check systemd or start it
        print("Auto-brightness daemon is not running.")
        print("Starting via: systemctl --user start nabu-autobrightness")
        subprocess.run(["systemctl", "--user", "start", "nabu-autobrightness"])
        time.sleep(0.5)
        res = send_ipc_command("status")
        if res:
            print(f"Auto-brightness is now \033[32mEnabled\033[0m.")
        return

    en = res.get("active", False)
    print(f"Auto-brightness is now {'\033[32mEnabled\033[0m' if en else '\033[31mDisabled\033[0m'}.")


def cli_enable():
    res = send_ipc_command("enable")
    if res:
        print("Auto-brightness \033[32mEnabled\033[0m.")
    else:
        print("Failed to reach daemon. Make sure nabu-autobrightness.service is running.")


def cli_disable():
    res = send_ipc_command("disable")
    if res:
        print("Auto-brightness \033[31mDisabled\033[0m.")
    else:
        print("Failed to reach daemon. Make sure nabu-autobrightness.service is running.")


def cli_set_profile(p):
    if p not in PROFILES:
        print(f"Invalid profile '{p}'. Available profiles:")
        for name, data in PROFILES.items():
            print(f"  - {name:10} : {data['description']}")
        return

    res = send_ipc_command("set_profile", profile=p)
    if res and res.get("success"):
        print(f"Profile set to '\033[36m{p}\033[0m' ({PROFILES[p]['description']}).")
    else:
        print("Failed to set profile.")


def cli_set_bias(val):
    try:
        bias = float(val)
    except ValueError:
        print("Error: bias must be a number (e.g. +10 or -5).")
        return

    res = send_ipc_command("set_bias", bias=bias)
    if res and res.get("success"):
        print(f"User preference bias set to {bias:+.1f}%.")
    else:
        print("Failed to set bias.")


def cli_reset_bias():
    res = send_ipc_command("reset_bias")
    if res and res.get("success"):
        print("User preference bias reset to 0.0%.")
    else:
        print("Failed to reset bias.")


def cli_test_monitor():
    """Live monitor of ambient light sensor and curve calculations without modifying backlight."""
    print("\033[1mAmbient Light Monitor (Test Mode)\033[0m")
    print("Reading from net.hadess.SensorProxy... Press Ctrl+C to exit.\n")

    try:
        system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        proxy = Gio.DBusProxy.new_sync(
            system_bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "net.hadess.SensorProxy",
            "/net/hadess/SensorProxy",
            "net.hadess.SensorProxy",
            None,
        )
        proxy.call_sync("ClaimLight", None, Gio.DBusCallFlags.NONE, 2000, None)
    except Exception as e:
        print(f"Error claiming sensor: {e}")
        return

    smoothed_lux = None
    try:
        while True:
            prop = proxy.get_cached_property("LightLevel")
            raw_lux = float(prop.get_double()) if prop else 0.0

            if smoothed_lux is None:
                smoothed_lux = raw_lux
            else:
                alpha = 0.22 if raw_lux > smoothed_lux else 0.08
                smoothed_lux = alpha * raw_lux + (1.0 - alpha) * smoothed_lux

            # Compute standard calibrated curve
            std_pct = calculate_profile_brightness_pct(smoothed_lux, "standard")

            print(
                f"\r\033[K  Raw: \033[33m{raw_lux:6.1f} lx\033[0m | "
                f"Smoothed: \033[36m{smoothed_lux:6.1f} lx\033[0m | "
                f"Calc Brightness: \033[32m{std_pct:5.1f}%\033[0m "
                f"({int(round(std_pct * 100))}/10000)",
                end="",
                flush=True,
            )
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n\nExiting test mode.")
    finally:
        try:
            proxy.call_sync("ReleaseLight", None, Gio.DBusCallFlags.NONE, 1000, None)
        except Exception:
            pass


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Ambient-light auto-brightness (DMS IPC / PowerDevil / sysfs)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Profiles:
  standard   Ergonomic human-calibrated curve for indoor and outdoor comfort (default)
  bright     Vibrant boost for well-lit rooms and outdoor visibility
  dim        Lower glare curve for night reading and dark environments
  battery    Power-saving curve capped at 68% max brightness

Examples:
  nabu-autobrightness                Display live status
  nabu-autobrightness --toggle       Toggle auto-brightness on/off
  nabu-autobrightness --profile bright Switch to bright profile
  nabu-autobrightness --bias +10     Add +10% personal brightness bias
  nabu-autobrightness --test         Live sensor reading test mode
        """,
    )

    parser.add_argument("command", nargs="?", choices=["status", "toggle", "enable", "disable", "test"], help="Optional command")
    parser.add_argument("-d", "--daemon", action="store_true", help="Run in background daemon mode")
    parser.add_argument("-s", "--status", action="store_true", help="Show current daemon & brightness status")
    parser.add_argument("-t", "--toggle", action="store_true", help="Toggle auto-brightness on/off")
    parser.add_argument("--enable", action="store_true", help="Enable auto-brightness")
    parser.add_argument("--disable", action="store_true", help="Disable auto-brightness")
    parser.add_argument("--profile", type=str, choices=list(PROFILES.keys()), help="Set active brightness profile")
    parser.add_argument("--bias", type=float, help="Set user preference bias percentage (-35 to +35)")
    parser.add_argument("--reset-bias", action="store_true", help="Reset user preference bias back to 0.0%%")
    parser.add_argument("--test", action="store_true", help="Monitor sensor values in real-time without changing screen")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")

    args = parser.parse_args()

    if args.daemon:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        daemon = AutoBrightnessDaemon()
        daemon.start()
        loop = GLib.MainLoop()
        try:
            loop.run()
        except KeyboardInterrupt:
            daemon._on_signal_exit()

    elif args.toggle or args.command == "toggle":
        cli_toggle()
    elif args.enable or args.command == "enable":
        cli_enable()
    elif args.disable or args.command == "disable":
        cli_disable()
    elif args.profile:
        cli_set_profile(args.profile)
    elif args.bias is not None:
        cli_set_bias(args.bias)
    elif args.reset_bias:
        cli_reset_bias()
    elif args.test or args.command == "test":
        cli_test_monitor()
    else:
        # Default action is --status or 'status'
        cli_status()


if __name__ == "__main__":
    main()
