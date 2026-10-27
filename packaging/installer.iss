; Inno Setup script for the 64-bit Windows installer.
; Built by packaging\build_installer.ps1, which first builds dist\TechXTerminal\ and passes the
; version in as AppVersion. To build by hand:  ISCC /DAppVersion=0.3.0 packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Tech X Terminal"
#define AppExe "TechXTerminal.exe"

[Setup]
; Never change AppId: Windows uses it to recognise an earlier installation to upgrade or remove.
AppId={{E0E6A419-D32E-4A1D-8C92-514F302D7230}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Venkata S Kuntamukkala
AppCopyright=Copyright (c) 2026 Venkata S Kuntamukkala. GNU GPL v3 licence.
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 64-bit only: installs into Program Files, not Program Files (x86), and refuses 32-bit Windows.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Installs for everyone by default; the first page offers "only for me", which needs no administrator.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=..\LICENSE
SetupIconFile=techx.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=..\dist
OutputBaseFilename=TechXTerminal-Setup-{#AppVersion}-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; An open Tech X Terminal is asked to close before its files are replaced.
CloseApplications=yes
RestartApplications=no
; Windows keeps showing a program's old icon after an upgrade, because the path stays the same;
; this tells Explorer at the end of the install to load the icons afresh.
ChangesAssociations=yes
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\TechXTerminal\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; The shortcuts used to be called "TechX Terminal"; an upgrade replaces them rather than adding a second set.
Type: files; Name: "{autoprograms}\TechX Terminal.lnk"
Type: files; Name: "{autodesktop}\TechX Terminal.lnk"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Saved sessions and settings in %USERPROFILE%\.techxterminal and the registry are left in place on
; uninstall, so reinstalling or upgrading keeps them. Passwords stay in Windows Credential Manager.
