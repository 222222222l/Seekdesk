"""Notification delivery options for a personal workbench."""

import base64
import os
from pathlib import Path
import subprocess
from xml.etree import ElementTree as ET

from .platforms import NeedsSetup


MAIL_HOSTS = {"qq.com": "smtp.qq.com", "163.com": "smtp.163.com", "126.com": "smtp.126.com"}


def mail_connection(cfg):
    if cfg["mail_mode"] == "custom":
        return cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_user"]
    address = cfg["email"]
    return MAIL_HOSTS.get(address.rpartition("@")[2].lower(), ""), 465, address


def mail_ready(cfg):
    host, _, sender = mail_connection(cfg)
    return bool(
        cfg["email_enabled"]
        and host
        and sender
        and cfg["email"]
        and (cfg["mail_mode"] == "custom" or cfg["smtp_password"])
    )


_TOAST_SCRIPT = """
$ErrorActionPreference='Stop'
$null=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]
$null=[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime]
$null=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]
$doc=New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::In.ReadToEnd())))
$notifier=[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('SeekDesk.JobWorkbench')
try {
    if ($notifier.get_Setting() -ne 'Enabled') { exit 3 }
} catch {
    # Windows 10 creates the notification settings entry on the first Show.
    if ($_.Exception.GetBaseException().HResult -ne [int]0x80070490) { throw }
}
$notifier.Show([Windows.UI.Notifications.ToastNotification]::new($doc))
if ($notifier.get_Setting() -ne 'Enabled') { exit 3 }
"""


def desktop_notice(title, body):
    if os.name != "nt":
        raise NeedsSetup("Windows 桌面提醒需要工作台运行在 Windows 上")
    import winreg

    xml = ET.Element("toast")
    binding = ET.SubElement(ET.SubElement(xml, "visual"), "binding", template="ToastGeneric")
    for value in (title, body):
        ET.SubElement(binding, "text").text = "".join(c for c in value if c.isprintable() or c == "\n")[:220]
    data = base64.b64encode(ET.tostring(xml, encoding="utf-8"))
    try:
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\SeekDesk.JobWorkbench"
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "求职工作台")
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _TOAST_SCRIPT],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        raise NeedsSetup("Windows 通知服务未就绪，请检查当前桌面会话") from exc
    if result.returncode == 3:
        raise NeedsSetup("Windows 已关闭通知，请在系统设置 → 通知中允许「求职工作台」")
    if result.returncode:
        raise NeedsSetup("Windows 未接受桌面提醒，请在已登录的桌面会话中测试通知")
    return "已提交到 Windows 通知中心"
