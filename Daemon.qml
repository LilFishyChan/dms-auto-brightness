import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Modules.Plugins

// 常驻守护进程 surface：跟 UI 可见性无关。
PluginComponent {
    id: root

    readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")

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
}
