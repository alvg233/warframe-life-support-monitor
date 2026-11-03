; 屏幕数值监控 —— Inno Setup 安装脚本（标准 Windows 安装器）
; 编译：build_installer.bat（或 ISCC.exe installer.iss）

#define MyAppName "屏幕数值监控"
#define MyAppVersion "1.9.3"
#define MyAppPublisher "ChengY"
#define MyAppExeName "ScreenOcrAlert.exe"
#define MyAppId "{{8B1C1D42-3F1E-4C77-9A55-2D6E6C2B7A10}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/alvg233/warframe-life-support-monitor
AppSupportURL=https://github.com/alvg233/warframe-life-support-monitor/issues
AppUpdatesURL=https://github.com/alvg233/warframe-life-support-monitor/releases
; 按用户安装，不需要管理员权限；安装时会让用户选择"仅我"或"所有用户"
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\ScreenOcrAlert
DefaultGroupName={#MyAppName}
; 允许用户自己选择安装路径
DisableDirPage=no
DisableWelcomePage=no
ShowLanguageDialog=no
DisableProgramGroupPage=yes
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
OutputDir=dist\installer
OutputBaseFilename=ScreenOcrAlert-Setup-{#MyAppVersion}
SetupIconFile=dist\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 数据目录在 %APPDATA%\ScreenOcrAlert，卸载时默认保留

[Languages]
Name: "chinesesimplified"; MessagesFile: "installer_lang\ChineseSimplified.isl"

[Tasks]
; 默认勾选，用户可自行取消
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce
Name: "startmenu"; Description: "创建开始菜单快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce

[Files]
Source: "dist\ScreenOcrAlert\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "config.json,config.json.bak,logs\*,snapshots\*,startup.log,使用说明.txt"
Source: "docs\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startmenu
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"; Tasks: startmenu
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清程序目录，用户数据（截图/日志/配置）保留在 %APPDATA%\ScreenOcrAlert
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\snapshots"
