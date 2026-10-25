import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Modules.Plugins
import qs.Services

// 常驻守护进程 surface：跟 UI 可见性无关。
//
// 除了跑 Python daemon，还把亮度的**变化历史**写成探针文件：
//   "<0-100> <unix 时间戳>"，每行一条，保留最近 20 条。
//
// 为什么必须是历史而不是当前值：daemon 通过 `dms ipc` 写亮度，写完探针也会
// 跟着变成它自己的值——只看当前值永远分不清"这一下是用户拖的还是 daemon 写的"。
// 有历史之后，daemon 可以拿自己写过的值逐条比对，找出真正来自用户的变化。
PluginComponent {
    id: root

    readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")
    readonly property string probePath: Quickshell.env("XDG_RUNTIME_DIR") + "/nabu-autobrightness/brightness.txt"

    Process {
        id: daemonProc
        command: ["python3", root.pluginDir + "scripts/nabu-autobrightness.py", "--daemon"]
        running: true
        onExited: (code, status) => {
            if (code !== 0 && code !== 143)
                restartTimer.start()
        }
    }

    Timer {
        id: restartTimer
        interval: 3000
        repeat: false
        onTriggered: daemonProc.running = true
    }

    // ==================== 亮度历史探针 ====================
    property var history: []

    FileView {
        id: probeFile
        path: root.probePath
        blockLoading: true
        printErrors: false
    }

    function record() {
        const v = BrightnessService.brightnessLevel
        const ts = Date.now() / 1000
        // 同一亮度短时间重复变化就跳过，别把历史挤满
        if (root.history.length > 0) {
            const last = root.history[root.history.length - 1]
            if (last[0] === v && ts - last[1] < 0.2)
                return
        }
        root.history = root.history.concat([[v, ts]]).slice(-20)
        let out = ""
        for (let i = 0; i < root.history.length; i++)
            out += root.history[i][0] + " " + root.history[i][1].toFixed(2) + "\n"
        probeFile.setText(out)
    }

    Connections {
        target: BrightnessService
        function onBrightnessVersionChanged() { root.record() }
        function onBrightnessLevelChanged() { root.record() }
    }

    Process {
        id: probeInit
        command: []
    }

    Component.onCompleted: {
        // 先建目录，否则首次写入会因为父目录不存在而失败
        probeInit.command = ["sh", "-c", "mkdir -p \"$(dirname " + root.probePath + ")\""]
        probeInit.running = true
        record()
    }
}
