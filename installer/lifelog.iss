; Instalador do Lifelog.
;
; Instala por usuario (sem exigir administrador) e registra a captura para
; subir no logon. Os dados ficam em %LOCALAPPDATA%\Lifelog e sobrevivem a
; desinstalacao — gravacao de meses nao pode sumir por engano.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "Lifelog"
#define MyAppExe "Lifelog.exe"
#define MyServerExe "LifelogServer.exe"
#define MyUiExe "LifelogUI.exe"
#define MyPublisher "joaosouz4dev"
#define MyURL "https://github.com/joaosouz4dev/lifelog"

[Setup]
AppId={{7F3C1A94-6D2E-4B55-9E1A-5C8B0D3F2A71}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyPublisher}
AppPublisherURL={#MyURL}
AppSupportURL={#MyURL}/issues
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=Lifelog-Setup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Instalacao por usuario: nada aqui precisa de driver nem servico do sistema,
; e pedir UAC afastaria quem so quer testar.
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "startup"; Description: "Iniciar a captura junto com o Windows"; GroupDescription: "Inicializacao:"
Name: "desktopicon"; Description: "Criar atalho na area de trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked

[Files]
Source: "..\dist\Lifelog\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
; Janela propria em vez de um atalho de URL: abrir no navegador obriga a
; pessoa a lidar com a porta, e a aba se perde entre as outras.
Name: "{group}\Interface do Lifelog"; Filename: "{app}\{#MyUiExe}"
Name: "{group}\Desinstalar o {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon
; A bandeja sobe o servidor quando precisa, entao um atalho na inicializacao
; basta — nao ha servico para registrar.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Abrir o {#MyAppName} agora"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Encerra a captura antes de remover os arquivos, senao os .exe ficam em uso.
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#MyAppExe} /IM {#MyServerExe} /IM {#MyUiExe}"; Flags: runhidden; RunOnceId: "PararLifelog"

[Messages]
brazilianportuguese.WelcomeLabel2=Isto vai instalar o [name/ver] no seu computador.%n%nO Lifelog grava o microfone e o audio do sistema, transcreve tudo localmente e gera relatorios do seu dia.%n%nSuas gravacoes ficam apenas neste computador.

[Code]
// Aviso de privacidade antes de instalar. Um app que grava audio continuamente
// tem que dizer isso de forma explicita, nao escondido num EULA.
function InitializeSetup(): Boolean;
begin
  Result := MsgBox(
    'O Lifelog grava continuamente o seu microfone e o audio que sai do computador.' + #13#10#13#10 +
    'Isso inclui a voz de outras pessoas em chamadas e reunioes. No Brasil, gravar uma conversa da qual voce participa e legal, mas armazenar e transcrever terceiros exige cuidado com a LGPD.' + #13#10#13#10 +
    'Tudo fica gravado apenas neste computador, e voce pode pausar a captura a qualquer momento pelo icone na bandeja.' + #13#10#13#10 +
    'Deseja continuar?',
    mbConfirmation, MB_YESNO) = IDYES;
end;

// Os dados ficam fora de {app} de proposito: desinstalar nao pode apagar
// meses de gravacao. Perguntamos em vez de decidir pelo usuario.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Lifelog');
    if not DirExists(DataDir) then
      Exit;

    // Em modo silencioso o MsgBox nao aparece: o Inno devolve o botao padrao
    // (IDYES) sozinho, e as gravacoes sumiriam sem ninguem ter respondido
    // nada. Numa desinstalacao sem interface, manter os dados e a unica
    // escolha defensavel — apagar por engano nao tem desfazer.
    if UninstallSilent then
    begin
      Log('Desinstalacao silenciosa: dados preservados em ' + DataDir);
      Exit;
    end;

    if MsgBox(
      'Apagar tambem as gravacoes, transcricoes e relatorios?' + #13#10#13#10 +
      DataDir + #13#10#13#10 +
      'Escolha Nao para manter os dados e poder reinstalar depois.',
      mbConfirmation, MB_YESNO) = IDYES then
      DelTree(DataDir, True, True, True);
  end;
end;
