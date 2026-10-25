import QtQuick
import qs.Common
import qs.Widgets
import qs.Modules.Plugins

PluginSettings {
    id: settings
    pluginId: "nabuAutoBrightness"

    function tr(term) {
        return I18n.trFor("nabuAutoBrightness", term);
    }

    // ---- 启动阶段的写入守卫 ----
    // DMS 会在插件数据从磁盘加载之前就预创建设置页；此时每个 Setting 的
    // loadValue() 只能拿到 defaultValue，而 onValueChanged 会立刻把这个默认值
    // 保存回去，把磁盘上的真实设置冲掉（症状：重启 DMS 后设置全变回默认）。
    // 所以在数据就绪之前一律拦住写入，就绪后再统一重读一遍。
    property bool savingEnabled: false

    function saveValue(key, value) {
        if (!settings.savingEnabled)
            return
        if (pluginService && pluginService.savePluginData) {
            pluginService.savePluginData(pluginId, key, value)
            settingChanged()
        }
    }

    Timer {
        interval: 1200
        running: true
        repeat: false
        onTriggered: {
            // 此刻数据已就绪，先把所有控件重读成真实值
            const stack = [settings]
            while (stack.length > 0) {
                const item = stack.pop()
                if (typeof item.loadValue === "function")
                    item.loadValue()
                for (let i = 0; i < item.children.length; i++)
                    stack.push(item.children[i])
            }
            // 之后才允许写入
            settings.savingEnabled = true
        }
    }


    readonly property var profileOptions: [
        { label: settings.tr("Standard (balanced indoor/outdoor)"), value: "standard" },
        { label: settings.tr("Bright (well-lit rooms)"), value: "bright" },
        { label: settings.tr("Dim (night reading)"), value: "dim" },
        { label: settings.tr("Battery saver (capped at 68%)"), value: "battery" }
    ]

    StyledText {
        width: parent.width
        text: settings.tr("Toggle it from the control center. Changes take effect immediately; the backlight backend is auto-detected (DMS IPC → PowerDevil → sysfs).")
        font.pixelSize: Theme.fontSizeMedium
        color: Theme.surfaceVariantText
        wrapMode: Text.WordWrap
    }

    // ================= 亮度曲线 =================
    StyledText {
        width: parent.width
        text: settings.tr("Brightness curve")
        font.pixelSize: Theme.fontSizeMedium
        font.bold: true
        color: Theme.surfaceText
        topPadding: Theme.spacingM
    }

    SelectionSetting {
        settingKey: "profile"
        label: settings.tr("Curve")
        description: settings.tr("Mapping from ambient light to screen brightness; switching profiles recalculates immediately from the current light level")
        defaultValue: "standard"
        options: settings.profileOptions
    }

    // ================= 说明 =================
    StyledText {
        width: parent.width
        text: settings.tr("Dragging the brightness slider makes the daemon remember your preference and keep applying it as an offset (user_bias, range ±35%). To clear the offset, set the brightness where you like it, then turn auto-brightness off and on again.")
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.surfaceVariantText
        wrapMode: Text.WordWrap
        topPadding: Theme.spacingM
    }
}
