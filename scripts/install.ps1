<#
.SYNOPSIS
    Instala o Lifelog para iniciar junto com o Windows.

.DESCRIPTION
    Registra duas Tarefas Agendadas no nivel do usuario (nao exigem
    administrador): o servidor, que sobe sem janela no logon, e a bandeja,
    que aparece na area de notificacao com o controle de pausa.

    As mensagens do console evitam acentos de proposito: o PowerShell 5 do
    Windows nao le UTF-8 de forma confiavel em script, e texto acentuado sai
    corrompido na tela.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -StartNow
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Status
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'

$TASK_SERVER = 'Lifelog - Servidor'
$TASK_TRAY   = 'Lifelog - Captura'
$ROOT = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Get-PythonwPath {
    # pythonw roda sem janela de console: sem ele, cada logon abriria dois
    # terminais na tela.
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw "Python nao encontrado no PATH." }
    $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
    if (-not (Test-Path $pyw)) { return $py }
    return $pyw
}

function Show-Status {
    Write-Host ""
    Write-Host "Estado das tarefas:" -ForegroundColor Cyan
    foreach ($name in @($TASK_SERVER, $TASK_TRAY)) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            $info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
            $last = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 1999) {
                $info.LastRunTime.ToString('dd/MM HH:mm')
            } else { 'nunca' }
            Write-Host ("  {0,-22} {1,-10} (ultima execucao: {2})" -f $name, $task.State, $last)
        } else {
            Write-Host ("  {0,-22} nao instalada" -f $name) -ForegroundColor DarkGray
        }
    }

    # O que importa de verdade: o servidor responde?
    try {
        $health = Invoke-WebRequest 'http://127.0.0.1:8000/health' -TimeoutSec 3 -UseBasicParsing
        Write-Host "  servidor respondendo   OK ($($health.StatusCode))" -ForegroundColor Green
    } catch {
        Write-Host "  servidor respondendo   nao" -ForegroundColor DarkGray
    }
    Write-Host ""
}

function Remove-Tasks {
    foreach ($name in @($TASK_SERVER, $TASK_TRAY)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "  removida: $name" -ForegroundColor Yellow
        }
    }
}

# ------------------------------- execucao -------------------------------

if ($Status) { Show-Status; exit 0 }

if ($Uninstall) {
    Write-Host ""
    Write-Host "Desinstalando o Lifelog..." -ForegroundColor Cyan
    Remove-Tasks
    Write-Host ""
    Write-Host "Pronto. Seus dados em data\ nao foram tocados." -ForegroundColor Green
    Write-Host ""
    exit 0
}

Write-Host ""
Write-Host "Instalando o Lifelog..." -ForegroundColor Cyan
Write-Host "  projeto: $ROOT"

$pythonw = Get-PythonwPath
Write-Host "  python : $pythonw"

# Reinstalar por cima e o caso comum (mudou de pasta, atualizou o codigo).
Remove-Tasks

# O servidor sobe por um lancador em vez de `powershell -Command "...uvicorn"`:
# aquele wrapper encerra quando a linha retorna e derruba o filho junto,
# deixando a tarefa com resultado 0 e nada rodando. O lancador tambem resolve
# o PYTHONPATH, que o uvicorn precisa para achar o pacote `server`.
$launcher = Join-Path $ROOT 'scripts\run_server.py'

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

# -- servidor --
Register-ScheduledTask -TaskName $TASK_SERVER -Force `
    -Action (New-ScheduledTaskAction -Execute $pythonw `
        -Argument "`"$launcher`"" `
        -WorkingDirectory $ROOT) `
    -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Servidor do Lifelog: transcricao, relatorios e interface web.' | Out-Null
Write-Host "  registrada: $TASK_SERVER" -ForegroundColor Green

# -- bandeja --
# 30s de atraso para o servidor estar de pe quando a captura comecar; sem isso
# o primeiro minuto de audio ficaria em fila esperando o backoff.
$trayTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trayTrigger.Delay = 'PT30S'

Register-ScheduledTask -TaskName $TASK_TRAY -Force `
    -Action (New-ScheduledTaskAction -Execute $pythonw `
        -Argument "`"$ROOT\windows-client\tray.py`"" `
        -WorkingDirectory $ROOT) `
    -Trigger $trayTrigger -Settings $settings -Principal $principal `
    -Description 'Captura de audio do Lifelog, com icone de pausa na bandeja.' | Out-Null
Write-Host "  registrada: $TASK_TRAY" -ForegroundColor Green

if ($StartNow) {
    Write-Host ""
    Write-Host "Iniciando agora..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName $TASK_SERVER
    Start-Sleep -Seconds 15
    Start-ScheduledTask -TaskName $TASK_TRAY
    Start-Sleep -Seconds 10
}

Show-Status

Write-Host "A captura sobe sozinha no proximo logon." -ForegroundColor Green
Write-Host "  Interface : http://localhost:8000"
Write-Host "  Pausar    : icone do Lifelog na bandeja (clique duplo)"
Write-Host "  Log       : data\server.log"
Write-Host "  Desligar  : powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall"
Write-Host ""
