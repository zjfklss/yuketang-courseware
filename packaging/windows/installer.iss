; Inno Setup 安装包脚本（可选）。
; 用法：先用 build-windows.ps1 打出 dist\yuketang-courseware\，再：
;   iscc packaging\windows\installer.iss
; 产物在 dist\yuketang-courseware-<版本>-setup.exe。

#define MyAppName "雨课堂课件"
#define MyAppVersion "0.5.6"
#define MyAppPublisher "本地工具"
#define MyAppExeName "yuketang-courseware.exe"

[Setup]
AppId={{6F1B2D64-9A3C-4C58-8C2E-3A7B1D9E5F02}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\yuketang-courseware
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=yuketang-courseware-{#MyAppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 装到用户目录，不需要管理员权限
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "..\..\dist\yuketang-courseware\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\命令行（雨课堂课件）"; Filename: "{app}\yuketang-courseware-cli.exe"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动{#MyAppName}"; Flags: nowait postinstall skipifsilent
