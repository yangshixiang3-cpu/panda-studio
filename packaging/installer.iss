; Panda Studio（曾用名「白底工作台」「panda工作台」）安装程序（Inno Setup 6）。由 packaging\build.ps1 调用编译。
; - 可选安装目录，默认装到第一个不是 C 盘的本地硬盘（如 D:\Panda Studio）；没有其他硬盘时才用 C 盘。
;   AppId 不变：从旧版本升级时沿用原来的安装目录（UsePreviousAppDir），并删掉旧名字的快捷方式和旧程序文件。
; - 程序文件从 WhiteBGStudio.exe 改名为 PandaStudio.exe：Windows 按程序路径缓存图标，换了文件名桌面图标才会刷新成熊猫。
; - 按当前用户安装，不需要管理员权限。选了 C 盘的目录会提醒一次（建议装到其他盘），仍可继续。
; - 版本号来自仓库根目录的 VERSION，由 build.ps1 用 /DAppVersion=<版本> 传入；单独编译时用下面的默认值。
; - config.json、参考库清单 refs\library.json 只在第一次安装时放入，升级重装不会覆盖；内置材质参考（refs 下的图片和 builtin.json）每次都更新；
;   卸载与升级时保留数据（exchange、用户的参考库清单、设置、secrets 密钥与 webview-data 缓存）。
#define AppName "Panda Studio"
#ifndef AppVersion
  #define AppVersion "1.7.2"
#endif
#define AppExe "PandaStudio.exe"
#define DistDir "dist\PandaStudio"

[Setup]
AppId={{F6395D14-DD77-4F34-8548-CB6FF5244EE1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Panda Studio
DefaultDirName={code:DefaultInstallDir}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=PandaStudio-安装程序-{#AppVersion}
SetupIconFile=icon\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他："

[InstallDelete]
; 旧名字（白底工作台、panda工作台）的快捷方式和旧程序文件，升级时删掉，换成 Panda Studio 的
Type: files; Name: "{autodesktop}\白底工作台.lnk"
Type: files; Name: "{autoprograms}\白底工作台.lnk"
Type: files; Name: "{autodesktop}\panda工作台.lnk"
Type: files; Name: "{autoprograms}\panda工作台.lnk"
Type: files; Name: "{app}\WhiteBGStudio.exe"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Excludes: "\config.json,\refs\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#DistDir}\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
; 内置材质参考（图片 + builtin.json）每次安装/升级都覆盖；用户的 library.json（认可状态、自己加的图）只在第一次放入
Source: "{#DistDir}\refs\*"; DestDir: "{app}\refs"; Excludes: "\library.json"; Flags: ignoreversion recursesubdirs createallsubdirs
; 公开版（panda-studio 公开库）不带 library.json，所以源文件不存在时跳过
Source: "{#DistDir}\refs\library.json"; DestDir: "{app}\refs"; Flags: onlyifdoesntexist uninsneveruninstall skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即打开{#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function GetDriveType(lpRootPathName: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';

const
  DRIVE_FIXED = 3;

{ 默认安装目录：第一个不是 C 盘的本地硬盘；都没有时才放到当前用户的程序目录 }
function DefaultInstallDir(Param: String): String;
var
  Letter: Integer;
  Root: String;
begin
  Result := ExpandConstant('{localappdata}\Programs\{#AppName}');
  for Letter := Ord('D') to Ord('Z') do
  begin
    Root := Chr(Letter) + ':\';
    if (GetDriveType(Root) = DRIVE_FIXED) and DirExists(Root) then
    begin
      Result := Root + '{#AppName}';
      Exit;
    end;
  end;
end;

{ 界面依赖 Microsoft Edge WebView2 运行库（Windows 11 自带）；没有时提醒一下，不阻止安装 }
function WebView2Installed(): Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
  Result := Result and (Version <> '') and (Version <> '0.0.0.0');
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Installed() then
    MsgBox('这台电脑没有检测到 Microsoft Edge WebView2 运行库，Panda Studio 的界面需要它。' + #13#10 +
           '安装完成后如果打不开，请到微软官网搜索「WebView2 Runtime」安装。', mbInformation, MB_OK);
end;

{ 选了 C 盘的目录：提醒建议装到其他盘（任务和出图都存在安装目录里，占空间），用户坚持就继续 }
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and (CompareText(Copy(Trim(WizardDirValue), 1, 2), 'C:') = 0) then
    Result := MsgBox('你选择装在 C 盘。Panda Studio 的任务和出图都保存在安装目录里，时间长了会占用不少空间，' +
                     '建议装到 D 盘等其他硬盘。' + #13#10#13#10 + '仍然装在 C 盘吗？', mbConfirmation, MB_YESNO) = IDYES;
end;

{ 卸载时：取消「渲染机」的开机自启（这台电脑设为渲染机时登记在当前用户的 Run 键里） }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'PandaStudioRenderNode');
end;
