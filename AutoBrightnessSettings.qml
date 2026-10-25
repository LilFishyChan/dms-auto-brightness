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
