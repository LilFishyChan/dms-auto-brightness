import QtQuick
import qs.Common
import qs.Widgets
import qs.DCommon.Widgets as DCommon
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


    // 滑块 + 右侧重置图标；只在值偏离默认值时出现
    component ResettableSlider: Column {
        id: rs
        property string settingKey
        property string label
        property string description: ""
        property int defaultValue: 0
        property int minimum: 0
        property int maximum: 100
        property string unit: ""

        width: parent.width
        spacing: 0

        SliderSetting {
            id: slider
            width: rs.width
            settingKey: rs.settingKey
            label: rs.label
            description: rs.description
            defaultValue: rs.defaultValue
            minimum: rs.minimum
            maximum: rs.maximum
            unit: rs.unit
        }

        Item {
            width: rs.width
            height: 20
            visible: slider.value !== rs.defaultValue

            DCommon.DIcon {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                name: "restart_alt"
                size: 15
                color: Theme.primary

                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -6
                    cursorShape: Qt.PointingHandCursor
                    onClicked: slider.value = rs.defaultValue
                }
            }
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

    // ================= 调节节奏 =================
    StyledText {
        width: parent.width
        text: settings.tr("Adaptation")
        font.pixelSize: Theme.fontSizeMedium
        font.bold: true
        color: Theme.surfaceText
        topPadding: Theme.spacingM
    }

    ResettableSlider {
        settingKey: "trigger_interval_s"
        label: settings.tr("Update interval")
        description: settings.tr("Minimum time between brightness changes. Each step is a DMS call, so a short interval makes the screen twitch and fights manual adjustments")
        defaultValue: 5
        minimum: 1
        maximum: 30
        unit: " s"
    }

    ResettableSlider {
        settingKey: "learn_rate_pct"
        label: settings.tr("Learning rate")
        description: settings.tr("How far each manual adjustment shifts the learned preference. Low values need several adjustments in the same direction before the curve follows your taste")
        defaultValue: 30
        minimum: 5
        maximum: 100
        unit: "%"
    }

    ResettableSlider {
        settingKey: "lux_deadband_pct"
        label: settings.tr("Light deadband")
        description: settings.tr("Skip adjustments when the averaged light changed less than this share")
        defaultValue: 10
        minimum: 5
        maximum: 20
        unit: "%"
    }

    // ================= 说明 =================
    StyledText {
        width: parent.width
        text: settings.tr("Dragging the brightness slider hands control back to you: auto-adjustment pauses for 3 seconds while you adjust, then takes the value you picked as the new baseline at the current light level and keeps adapting from there. Run nabu-autobrightness --reset-bias to drop the baseline and return to the plain curve.")
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.surfaceVariantText
        wrapMode: Text.WordWrap
        topPadding: Theme.spacingM
    }
}
