[CmdletBinding()]
param(
    [string]$NapCatDir = $env:NAPCAT_DIR,
    [string]$QQUin = $env:QQ_UIN,
    [switch]$ValidateOnly,
    [switch]$SmokeTest
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($NapCatDir)) {
    $workspaceRoot = Split-Path -Parent $projectRoot
    $NapCatDir = Join-Path $workspaceRoot 'NapCat.Shell'
}

$NapCatDir = [System.IO.Path]::GetFullPath($NapCatDir)
$napcatLauncher = Join-Path $NapCatDir 'launcher-user.bat'
$nonebotExecutable = Join-Path $projectRoot '.venv\Scripts\amadeus-bot.exe'

if (-not (Test-Path -LiteralPath $napcatLauncher -PathType Leaf)) {
    throw "NapCat launcher was not found: $napcatLauncher"
}
if (-not (Test-Path -LiteralPath $nonebotExecutable -PathType Leaf)) {
    throw "NoneBot executable was not found: $nonebotExecutable"
}
if ([string]::IsNullOrWhiteSpace($QQUin) -or $QQUin -notmatch '^\d+$') {
    throw 'QQ_UIN must be set to a numeric QQ account.'
}

if ($ValidateOnly) {
    Write-Output 'Bot tray configuration is valid.'
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class BotTrayNativeWindow
{
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr windowHandle, int command);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr windowHandle);
}
'@

[System.Windows.Forms.Application]::EnableVisualStyles()

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\AmadeusBotTrayManager', [ref]$createdNew)
if (-not $createdNew) {
    [System.Windows.Forms.MessageBox]::Show(
        'The bot tray manager is already running.',
        'Amadeus Bot',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    ) | Out-Null
    exit 0
}

$script:exitRequested = $false
$script:servicesStarted = $false
$script:smokeTestFailed = $false
$script:fatalError = $null
$script:services = @{}

$form = [System.Windows.Forms.Form]::new()
$form.Text = 'Amadeus Bot Console'
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.MinimumSize = [System.Drawing.Size]::new(720, 460)
$form.Size = [System.Drawing.Size]::new(980, 660)
$form.Icon = [System.Drawing.SystemIcons]::Application

$tabs = [System.Windows.Forms.TabControl]::new()
$tabs.Dock = [System.Windows.Forms.DockStyle]::Fill

$statusStrip = [System.Windows.Forms.StatusStrip]::new()
$statusStrip.SizingGrip = $false

function New-LogView {
    param([string]$Name)

    $page = [System.Windows.Forms.TabPage]::new($Name)
    $box = [System.Windows.Forms.RichTextBox]::new()
    $box.BackColor = [System.Drawing.Color]::FromArgb(18, 18, 18)
    $box.BorderStyle = [System.Windows.Forms.BorderStyle]::None
    $box.DetectUrls = $true
    $box.Dock = [System.Windows.Forms.DockStyle]::Fill
    $box.Font = [System.Drawing.Font]::new('Consolas', 10)
    $box.ForeColor = [System.Drawing.Color]::Gainsboro
    $box.ReadOnly = $true
    $box.WordWrap = $false
    $page.Controls.Add($box)
    $tabs.TabPages.Add($page) | Out-Null

    $label = [System.Windows.Forms.ToolStripStatusLabel]::new()
    $label.AutoSize = $false
    $label.Text = "${Name}: waiting"
    $label.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
    $label.Width = 260
    $statusStrip.Items.Add($label) | Out-Null

    return [pscustomobject]@{
        Name        = $Name
        Process     = $null
        Queue       = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
        OutputRead  = $null
        ErrorRead   = $null
        ExitReported = $false
        LogBox      = $box
        StatusLabel = $label
        WasRunning  = $false
    }
}

$script:services.NapCat = New-LogView -Name 'NapCat'
$script:services.NoneBot = New-LogView -Name 'NoneBot'
$form.Controls.Add($tabs)
$form.Controls.Add($statusStrip)

$notifyIcon = [System.Windows.Forms.NotifyIcon]::new()
$notifyIcon.Icon = [System.Drawing.SystemIcons]::Application
$notifyIcon.Text = 'Amadeus Bot'
$notifyIcon.Visible = $true

$trayMenu = [System.Windows.Forms.ContextMenuStrip]::new()
$showMenuItem = $trayMenu.Items.Add('Show console')
$trayMenu.Items.Add([System.Windows.Forms.ToolStripSeparator]::new()) | Out-Null
$exitMenuItem = $trayMenu.Items.Add('Exit completely')
$notifyIcon.ContextMenuStrip = $trayMenu

function Show-ConsoleWindow {
    if (-not $form.Visible) {
        $form.ShowInTaskbar = $true
        $form.Show()
    }
    if ($form.WindowState -eq [System.Windows.Forms.FormWindowState]::Minimized) {
        $form.WindowState = [System.Windows.Forms.FormWindowState]::Normal
    }
    [BotTrayNativeWindow]::ShowWindow($form.Handle, 9) | Out-Null
    [BotTrayNativeWindow]::SetForegroundWindow($form.Handle) | Out-Null
    $form.Activate()
}

function Hide-ConsoleWindow {
    $form.Hide()
    $form.ShowInTaskbar = $false
}

function Start-ManagedProcess {
    param(
        [string]$Name,
        [string]$FileName,
        [string]$Arguments,
        [string]$WorkingDirectory
    )

    $service = $script:services[$Name]
    $service.Queue.Enqueue("[$(Get-Date -Format 'HH:mm:ss')] Starting $Name...")

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FileName
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo

    if (-not $process.Start()) {
        throw "Failed to start $Name."
    }
    $service.Process = $process
    $service.OutputRead = $process.StandardOutput.ReadLineAsync()
    $service.ErrorRead = $process.StandardError.ReadLineAsync()
    $service.ExitReported = $false
}

function Read-AvailableOutput {
    param($Service)

    foreach ($stream in @(
        @{ Task = 'OutputRead'; Reader = $Service.Process.StandardOutput },
        @{ Task = 'ErrorRead'; Reader = $Service.Process.StandardError }
    )) {
        $readCount = 0
        $task = $Service.($stream.Task)
        while ($null -ne $task -and $task.IsCompleted -and $readCount -lt 200) {
            try {
                $line = $task.GetAwaiter().GetResult()
            }
            catch {
                $Service.Queue.Enqueue("Output reader failed: $($_.Exception.Message)")
                $Service.($stream.Task) = $null
                break
            }

            if ($null -eq $line) {
                $Service.($stream.Task) = $null
                break
            }

            $Service.Queue.Enqueue($line)
            $task = $stream.Reader.ReadLineAsync()
            $Service.($stream.Task) = $task
            $readCount++
        }
    }
}

function Invoke-TaskKill {
    param(
        [int]$ProcessId,
        [switch]$Force
    )

    $taskKill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $taskKill
    $startInfo.Arguments = if ($Force) { "/PID $ProcessId /T /F" } else { "/PID $ProcessId /T" }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $killer = [System.Diagnostics.Process]::Start($startInfo)
    $killer.WaitForExit(5000) | Out-Null
    $killer.Dispose()
}

function Stop-ManagedProcess {
    param([string]$Name)

    $service = $script:services[$Name]
    $process = $service.Process
    if ($null -eq $process) {
        return
    }

    try {
        if (-not $process.HasExited) {
            $service.Queue.Enqueue("[$(Get-Date -Format 'HH:mm:ss')] Stopping $Name...")
            Invoke-TaskKill -ProcessId $process.Id
            if (-not $process.WaitForExit(5000)) {
                Invoke-TaskKill -ProcessId $process.Id -Force
                $process.WaitForExit(5000) | Out-Null
            }
        }
    }
    catch {
        $service.Queue.Enqueue("[$(Get-Date -Format 'HH:mm:ss')] Failed to stop ${Name}: $($_.Exception.Message)")
    }
}

function Stop-AllServices {
    Stop-ManagedProcess -Name 'NoneBot'
    Stop-ManagedProcess -Name 'NapCat'
}

$flushTimer = [System.Windows.Forms.Timer]::new()
$flushTimer.Interval = 120
$flushTimer.add_Tick({
    foreach ($name in @('NapCat', 'NoneBot')) {
        $service = $script:services[$name]
        if ($null -ne $service.Process) {
            Read-AvailableOutput -Service $service
        }
        [string]$line = $null
        $lines = [System.Collections.Generic.List[string]]::new()
        while ($service.Queue.TryDequeue([ref]$line)) {
            $lines.Add($line)
            $line = $null
        }
        if ($lines.Count -gt 0) {
            $service.LogBox.AppendText(($lines -join [Environment]::NewLine) + [Environment]::NewLine)
            if ($service.LogBox.TextLength -gt 1000000) {
                $service.LogBox.Select(0, 250000)
                $service.LogBox.SelectedText = ''
            }
            $service.LogBox.SelectionStart = $service.LogBox.TextLength
            $service.LogBox.ScrollToCaret()
        }

        $running = $null -ne $service.Process -and -not $service.Process.HasExited
        if ($running) {
            $service.StatusLabel.Text = "${name}: running (PID $($service.Process.Id))"
            $service.StatusLabel.ForeColor = [System.Drawing.Color]::DarkGreen
        }
        elseif ($null -eq $service.Process) {
            $service.StatusLabel.Text = "${name}: waiting"
            $service.StatusLabel.ForeColor = [System.Drawing.Color]::DimGray
        }
        else {
            $service.StatusLabel.Text = "${name}: stopped"
            $service.StatusLabel.ForeColor = [System.Drawing.Color]::Firebrick
            if (-not $service.ExitReported) {
                $exitCode = try { $service.Process.ExitCode } catch { 'unknown' }
                $service.Queue.Enqueue("[$(Get-Date -Format 'HH:mm:ss')] $name exited (code $exitCode).")
                $service.ExitReported = $true
            }
            if ($service.WasRunning -and -not $script:exitRequested) {
                $notifyIcon.BalloonTipTitle = 'Amadeus Bot'
                $notifyIcon.BalloonTipText = "$name has stopped. Open the console for details."
                $notifyIcon.ShowBalloonTip(4000)
            }
        }
        $service.WasRunning = $running
    }
})

$startNoneBotTimer = [System.Windows.Forms.Timer]::new()
$startNoneBotTimer.Interval = 1000
$startNoneBotTimer.add_Tick({
    $startNoneBotTimer.Stop()
    try {
        Start-ManagedProcess -Name 'NoneBot' -FileName $nonebotExecutable -Arguments '' -WorkingDirectory $projectRoot
    }
    catch {
        $script:services.NoneBot.Queue.Enqueue("[$(Get-Date -Format 'HH:mm:ss')] NoneBot failed to start: $($_.Exception.Message)")
    }
})

$smokeTestTimer = [System.Windows.Forms.Timer]::new()
$smokeTestTimer.Interval = 1500
$smokeTestTimer.add_Tick({
    $smokeTestTimer.Stop()
    $napCatOutput = $script:services.NapCat.LogBox.Text
    $noneBotOutput = $script:services.NoneBot.LogBox.Text
    if ($napCatOutput -notmatch 'NapCat smoke test' -or $noneBotOutput -notmatch 'NoneBot smoke test') {
        $script:smokeTestFailed = $true
    }
    Hide-ConsoleWindow
    if ($form.Visible) {
        $script:smokeTestFailed = $true
    }
    Show-ConsoleWindow
    if (-not $form.Visible) {
        $script:smokeTestFailed = $true
    }
    $script:exitRequested = $true
    $flushTimer.Stop()
    Stop-AllServices
    $form.Close()
    [System.Windows.Forms.Application]::ExitThread()
})

$form.add_Shown({
    if (-not $SmokeTest) {
        Show-ConsoleWindow
    }
    if (-not $script:servicesStarted) {
        $script:servicesStarted = $true
        if ($SmokeTest) {
            Start-ManagedProcess -Name 'NapCat' -FileName $env:ComSpec -Arguments '/d /s /c "echo NapCat smoke test"' -WorkingDirectory $projectRoot
            Start-ManagedProcess -Name 'NoneBot' -FileName $env:ComSpec -Arguments '/d /s /c "echo NoneBot smoke test"' -WorkingDirectory $projectRoot
            $smokeTestTimer.Start()
            return
        }
        try {
            $commandArguments = "/d /s /c `"call launcher-user.bat $QQUin`""
            Start-ManagedProcess -Name 'NapCat' -FileName $env:ComSpec -Arguments $commandArguments -WorkingDirectory $NapCatDir
        }
        catch {
            $script:services.NapCat.Queue.Enqueue("[$(Get-Date -Format 'HH:mm:ss')] NapCat failed to start: $($_.Exception.Message)")
        }
        $startNoneBotTimer.Start()
    }
})

$form.add_FormClosing({
    param($sender, $eventArgs)
    if (-not $script:exitRequested) {
        $eventArgs.Cancel = $true
        Hide-ConsoleWindow
        $notifyIcon.BalloonTipTitle = 'Amadeus Bot is still running'
        $notifyIcon.BalloonTipText = 'Click the tray icon to restore the console. Right-click it to exit.'
        $notifyIcon.ShowBalloonTip(3000)
    }
})

$form.add_Resize({
    if ($form.WindowState -eq [System.Windows.Forms.FormWindowState]::Minimized) {
        Hide-ConsoleWindow
    }
})

$showAction = {
    Show-ConsoleWindow
}
$showMenuItem.add_Click($showAction)
$notifyIcon.add_MouseClick({
    param($sender, $eventArgs)
    if ($eventArgs.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        Show-ConsoleWindow
    }
})

$exitMenuItem.add_Click({
    $script:exitRequested = $true
    $notifyIcon.Visible = $false
    $startNoneBotTimer.Stop()
    $flushTimer.Stop()
    Stop-AllServices
    $form.Close()
    [System.Windows.Forms.Application]::ExitThread()
})

try {
    $flushTimer.Start()
    [System.Windows.Forms.Application]::Run($form)
}
catch {
    $script:fatalError = $_.Exception
    $logDirectory = Join-Path $projectRoot 'logs\runtime'
    $logPath = Join-Path $logDirectory 'tray-manager-error.log'
    try {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        Add-Content -LiteralPath $logPath -Encoding UTF8 -Value (
            "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $($_.Exception.ToString())"
        )
    }
    catch {
    }
    if (-not $SmokeTest) {
        [System.Windows.Forms.MessageBox]::Show(
            "The tray manager stopped unexpectedly. Details were written to:`n$logPath",
            'Amadeus Bot',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    }
}
finally {
    if (-not $script:exitRequested) {
        $script:exitRequested = $true
        Stop-AllServices
    }
    $notifyIcon.Visible = $false
    $notifyIcon.Dispose()
    $trayMenu.Dispose()
    $flushTimer.Dispose()
    $startNoneBotTimer.Dispose()
    $smokeTestTimer.Dispose()
    $form.Dispose()
    if ($createdNew) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}

if ($null -ne $script:fatalError) {
    Write-Error $script:fatalError.Message
    exit 1
}

if ($SmokeTest) {
    if ($script:smokeTestFailed) {
        Write-Error 'Tray manager smoke test failed.'
        exit 1
    }
    Write-Output 'Tray manager smoke test passed.'
}
