<#
.SYNOPSIS
    Optionally mark Elink traffic with a Windows QoS policy.

.DESCRIPTION
    This is an explicit, administrator-run helper. It does not reserve
    bandwidth and it cannot force Tailscale or DERP to preserve DSCP. Run it
    on both the control and controlled Windows machines if local policy
    priority is desired.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Enable', 'Disable')]
    [string]$Action,

    [Parameter(Mandatory = $false)]
    [string]$ExecutablePath = ""
)

$policyName = 'Elink-Interactive-Video'

if ($Action -eq 'Enable') {
    if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
        throw '请用 -ExecutablePath 指定 Elink.exe 的绝对路径。'
    }
    $resolved = (Resolve-Path -LiteralPath $ExecutablePath -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "找不到可执行文件：$resolved"
    }
    Remove-NetQosPolicy -Name $policyName -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue
    New-NetQosPolicy -Name $policyName `
        -AppPathNameMatchCondition $resolved `
        -IPProtocolMatchCondition Both `
        -DSCPAction 34 `
        -NetworkProfile All `
        -PolicyStore ActiveStore | Out-Host
    Write-Host "已启用 Elink AF41 QoS 标记：$resolved"
} else {
    Remove-NetQosPolicy -Name $policyName -PolicyStore ActiveStore -Confirm:$false
    Write-Host '已移除 Elink QoS 策略。'
}
