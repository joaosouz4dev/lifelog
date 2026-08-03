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
; Sem isto, instalar por cima com o Lifelog aberto trava num "os arquivos
; estao em uso, tente novamente" que so sai matando o processo na mao.
; O Inno fecha os processos que seguram os arquivos e os reabre no fim.
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "startup"; Description: "Iniciar a captura junto com o Windows"; GroupDescription: "Inicializacao:"
Name: "desktopicon"; Description: "Criar atalho na area de trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked

[Files]
Source: "..\dist\Lifelog\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Um atalho so: o Lifelog e a bandeja. A janela da interface (LifelogUI.exe)
; abre pelo menu da bandeja — um atalho proprio para ela aparecia no menu
; Iniciar como se fosse um segundo aplicativo, o que confunde mais do que ajuda.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Desinstalar o {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon
; A bandeja sobe o servidor quando precisa, entao um atalho na inicializacao
; basta — nao ha servico para registrar. O --startup silencia o aviso de
; "ja esta em execucao": no logon ninguem pediu para abrir, entao um popup
; modal seria so um estorvo a cada boot.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "--startup"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Abrir o {#MyAppName} agora"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Encerra a captura antes de remover os arquivos, senao os .exe ficam em uso.
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#MyAppExe} /IM {#MyServerExe} /IM {#MyUiExe}"; Flags: runhidden; RunOnceId: "PararLifelog"
; Quem testou pelo codigo-fonte tem tarefas agendadas do scripts\install.ps1,
; que rodam como pythonw.exe e sobrevivem ao taskkill acima. Sem apaga-las, o
; Lifelog ressuscita no proximo logon mesmo depois de desinstalado.
Filename: "{cmd}"; Parameters: "/C schtasks /Delete /TN ""Lifelog - Captura"" /F & schtasks /Delete /TN ""Lifelog - Servidor"" /F"; Flags: runhidden; RunOnceId: "RemoverTarefasAgendadas"

[Messages]
brazilianportuguese.WelcomeLabel2=Isto vai instalar o [name/ver] no seu computador.%n%nO Lifelog grava o microfone e o audio do sistema, transcreve tudo localmente e gera relatorios do seu dia.%n%nSuas gravacoes ficam apenas neste computador.

[Code]
// Encerra o Lifelog antes de sobrescrever os arquivos.
//
// O CloseApplications do Inno usa o Restart Manager, que nao enxerga
// processos sem janela — e o LifelogServer.exe roda invisivel. Sem este
// taskkill, instalar por cima trava num "os arquivos estao em uso, tente
// novamente" que so sai matando o processo na mao.
procedure PararLifelog();
var
  Codigo: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
       '/C taskkill /F /IM {#MyAppExe} /IM {#MyServerExe} /IM {#MyUiExe}',
       '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  // Um instante para o Windows liberar os handles dos arquivos.
  Sleep(1200);
end;

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

// ssInstall e o momento certo: o usuario ja confirmou tudo e os arquivos
// ainda nao comecaram a ser copiados.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    PararLifelog();
end;

// Os dados ficam fora de {app} de proposito: desinstalar nao pode apagar
// meses de gravacao. Perguntamos em vez de decidir pelo usuario.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  Codigo: Integer;
begin
  // Antes de remover: o [UninstallRun] roda tarde demais, quando o Inno ja
  // tentou apagar os arquivos e falhou por estarem em uso.
  if CurUninstallStep = usUninstall then
  begin
    Exec(ExpandConstant('{cmd}'),
         '/C taskkill /F /IM {#MyAppExe} /IM {#MyServerExe} /IM {#MyUiExe}',
         '', SW_HIDE, ewWaitUntilTerminated, Codigo);
    Sleep(1200);
  end;

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
