import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Services
import qs.Widgets
import qs.Modules.Plugins

PluginComponent {
    id: root

    function tr(term) {
        return I18n.trFor("nabuAutoBrightness", term);
    }

    readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")
    readonly property string script: root.pluginDir + "scripts/nabu-autobrightness.py"

    property bool isEnabled: false
    property bool busy: false

    // 守护进程在 Daemon.qml 的 daemon surface 里常驻，不随控制中心开关而启停。
    // ================= 控制中心开关 =================
    ccWidgetIcon: isEnabled ? "brightness_auto" : "brightness_5"
    ccWidgetPrimaryText: tr("Auto Brightness")
    ccWidgetSecondaryText: isEnabled ? tr("On") : tr("Off")
    ccWidgetIsActive: isEnabled

    onCcWidgetToggled: {
        const target = !isEnabled
        root.busy = true
        isEnabled = target
        toggleProc.command = ["python3", root.script,
                              target ? "--enable" : "--disable"]
        toggleProc.running = true
    }

    // ================= 状态查询 =================
    Process {
        id: queryProc
        command: ["python3", root.script, "status"]
        stdout: SplitParser {
            onRead: line => {
                if (line.indexOf("Auto-Brightness:") !== -1)
                    root.isEnabled = line.indexOf("Enabled") !== -1
            }
        }
    }

    Process {
        id: toggleProc
        command: []
        onExited: {
            root.busy = false
            queryProc.running = true
        }
    }

    Timer {
        interval: 8000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: if (!root.busy) queryProc.running = true
    }

    // ================= 栏内图标 =================
    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingS
            DankIcon {
                name: root.isEnabled ? "brightness_auto" : "brightness_5"
                size: Theme.iconSize
                color: root.isEnabled ? Theme.primary : Theme.surfaceVariantText
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    verticalBarPill: Component {
        DankIcon {
            name: root.isEnabled ? "brightness_auto" : "brightness_5"
            size: Theme.iconSize
            color: root.isEnabled ? Theme.primary : Theme.surfaceVariantText
            anchors.horizontalCenter: parent.horizontalCenter
        }
    }
}
