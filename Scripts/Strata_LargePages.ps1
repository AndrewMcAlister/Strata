# ==============================================================================================
#  Strata_LargePages.ps1 - give the Strata engine 2 MB pages for its expert arena on Windows.
#
#    Strata_LargePages.ps1 /status    say whether this PC can back the arena with large pages
#    Strata_LargePages.ps1 /add       grant the privilege to this account (needs admin)
#    Strata_LargePages.ps1 /remove    take it away again, restoring the default behaviour
#    Strata_LargePages.ps1 /?         this text
#
#  WHY THIS EXISTS
#
#  The expert arena is ~40 GB of host memory that the CPU walks expert by expert and the GPU DMAs
#  out of.  Windows can back it with 2 MB pages or with 4 KB pages, and the engine prefers large
#  pages - include\strata\core\pinned.hpp says why: "33.97 GB at 4 KB pages is 8.3 million TLB
#  entries, which does not fit in any TLB, so every block of every expert matvec takes TLB misses."
#
#  Large pages on Windows need SeLockMemoryPrivilege ("Lock pages in memory"), which a normal
#  account does not have - so the fallback is the COMMON case, not an error.  Every Strata start
#  on such a PC logs:
#
#    expert arena: cudaHostRegister PORTABLE ok; large pages refused for 42916118528 B
#                  (GetLargePageMinimum=2097152, VirtualAlloc error 1314); using 4 KB pages
#
#  Error 1314 is ERROR_PRIVILEGE_NOT_HELD.  src\core\pinned.cu already enables the privilege in its
#  own token and retries, so nothing in the engine needs changing: only the account's assignment is
#  missing.  That is the one thing this script touches.
#
#  GRANT IT TO THE ACCOUNT, NOT TO Administrators.  Windows builds the logon token from these
#  assignments, but under UAC a non-elevated process gets a FILTERED token that drops privileges
#  arriving through group membership and keeps those assigned to the account itself.  Assigning to
#  Administrators would therefore work only when Strata is started elevated; assigning to the user
#  is what makes a plain double-click on Strata_Start.bat enough.
#
#  A SIGN-OUT IS REQUIRED.  The privilege is baked into the token when you log on, so this script
#  cannot make it appear in a session that is already running - restarting Strata alone will still
#  log "4 KB pages".  Sign out and back in (or reboot), then /status can prove it took.
#
#  It uses LsaAddAccountRights rather than secedit on purpose: secedit rewrites the whole
#  USER_RIGHTS area of the local policy, while this adds one right to one account and /remove takes
#  exactly that back.  Nothing else in the security policy is read or written.
#
#  Exit codes: 0 the question was answered (or the change was made), 1 it could not be done,
#              2 a bad option, 3 not elevated when the action needed it.
# ==============================================================================================

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $Action = 'status',

    # The account to act on, as a SID string.  Defaults to whoever is running this.
    # It is passed explicitly when the script re-launches itself elevated: an elevated process may
    # be running as a DIFFERENT account (when a non-admin user answers the UAC prompt with admin
    # credentials), and the right belongs on the account that will start Strata, not on that one.
    [string] $AccountSid
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --- the LSA calls, and a direct test of the thing we actually care about ----------------------
#
# LsaEnumerateAccountRights answers "is the right assigned to this account" (a fact that survives
# logon).  That is not the same question as "can THIS process allocate large pages", which also
# needs the privilege to be present in the current token.  Rather than enumerate token privileges,
# the script simply tries the allocation the engine tries - 2 MB, MEM_LARGE_PAGES - and reports
# what Windows says.  Same call, same error numbers, nothing to interpret.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace StrataLsa {

    [StructLayout(LayoutKind.Sequential)]
    public struct LSA_UNICODE_STRING {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct LSA_OBJECT_ATTRIBUTES {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public int Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct LUID {
        public uint LowPart;
        public int HighPart;
    }

    // A TOKEN_PRIVILEGES holding exactly one LUID_AND_ATTRIBUTES, flattened.  The layout is
    // byte-for-byte the same (uint + LUID + uint); it is written out this way because assigning
    // through a nested struct field is awkward from PowerShell, and this needs no nested assignment.
    [StructLayout(LayoutKind.Sequential)]
    public struct TOKEN_PRIVILEGES {
        public uint PrivilegeCount;
        public uint LuidLowPart;
        public int LuidHighPart;
        public uint Attributes;
    }

    public static class Native {
        [DllImport("advapi32.dll", SetLastError = true)]
        public static extern uint LsaOpenPolicy(IntPtr SystemName, ref LSA_OBJECT_ATTRIBUTES ObjectAttributes,
                                                int AccessMask, out IntPtr PolicyHandle);

        [DllImport("advapi32.dll", SetLastError = true)]
        public static extern uint LsaAddAccountRights(IntPtr PolicyHandle, byte[] AccountSid,
                                                      LSA_UNICODE_STRING[] UserRights, int CountOfRights);

        [DllImport("advapi32.dll", SetLastError = true)]
        public static extern uint LsaRemoveAccountRights(IntPtr PolicyHandle, byte[] AccountSid, bool AllRights,
                                                         LSA_UNICODE_STRING[] UserRights, int CountOfRights);

        [DllImport("advapi32.dll", SetLastError = true)]
        public static extern uint LsaEnumerateAccountRights(IntPtr PolicyHandle, byte[] AccountSid,
                                                            out IntPtr UserRights, out int CountOfRights);

        [DllImport("advapi32.dll")]
        public static extern uint LsaFreeMemory(IntPtr Buffer);

        [DllImport("advapi32.dll")]
        public static extern uint LsaClose(IntPtr ObjectHandle);

        [DllImport("advapi32.dll")]
        public static extern int LsaNtStatusToWinError(uint Status);

        // SeLockMemoryPrivilege sits DISABLED in every token until the process enables it, so these
        // four calls come before any large-page allocation.  The engine does exactly this itself
        // (src\core\pinned.cu:71-83); a probe that skips it reports 1314 for an account that holds
        // the right perfectly well, which is a false negative that would never clear.
        [DllImport("kernel32.dll")]
        public static extern IntPtr GetCurrentProcess();

        [DllImport("advapi32.dll", SetLastError = true)]
        public static extern bool OpenProcessToken(IntPtr ProcessHandle, uint DesiredAccess,
                                                   out IntPtr TokenHandle);

        [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern bool LookupPrivilegeValueW(string SystemName, string Name, out LUID Luid);

        [DllImport("advapi32.dll", SetLastError = true)]
        public static extern bool AdjustTokenPrivileges(IntPtr TokenHandle, bool DisableAllPrivileges,
                                                        ref TOKEN_PRIVILEGES NewState, uint BufferLength,
                                                        IntPtr PreviousState, IntPtr ReturnLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr hObject);

        // The engine's own test, in miniature: MEM_RESERVE|MEM_COMMIT|MEM_LARGE_PAGES.
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr VirtualAlloc(IntPtr lpAddress, UIntPtr dwSize, uint flAllocationType,
                                                 uint flProtect);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool VirtualFree(IntPtr lpAddress, UIntPtr dwSize, uint dwFreeType);

        [DllImport("kernel32.dll")]
        public static extern UIntPtr GetLargePageMinimum();
    }
}
'@

$Native = [StrataLsa.Native]
$RightName = 'SeLockMemoryPrivilege'          # "Lock pages in memory" in the Local Security Policy UI
# Reading the assignments needs only lookup; writing them needs the wider mask, which also needs admin.
$PolicyLookupNames = 0x00000800
$PolicyAllAccess = 0x000F0FFF
$MemReserve = 0x00002000
$MemCommit = 0x00001000
$MemLargePages = 0x20000000
$MemRelease = 0x00008000
$PageReadWrite = 0x00000004
$TokenAdjustPrivileges = 0x0020
$TokenQuery = 0x0008
$SePrivilegeEnabled = 0x00000002
$ErrorNotAllAssigned = 1300                        # ERROR_NOT_ALL_ASSIGNED

# --- helpers ------------------------------------------------------------------------------------

function Get-CurrentSid {
    return ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
}

function Test-Elevated {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object System.Security.Principal.WindowsPrincipal $id).IsInRole(
        [System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-LsaString([string] $Text) {
    $us = New-Object StrataLsa.LSA_UNICODE_STRING
    $us.Buffer = [System.Runtime.InteropServices.Marshal]::StringToHGlobalUni($Text)
    $us.Length = [uint16]($Text.Length * 2)
    $us.MaximumLength = [uint16](($Text.Length + 1) * 2)
    return $us
}

# NOTE: the local must not be called $sid.  PowerShell variable names are case-insensitive, so $sid
# IS $Sid - the [string]-typed parameter - and assigning the SecurityIdentifier object into it would
# coerce it straight back to a string, leaving nothing with a BinaryLength.
function Get-SidBytes([string] $Sid) {
    $identifier = New-Object System.Security.Principal.SecurityIdentifier $Sid
    $bytes = [byte[]]::new($identifier.BinaryLength)
    $identifier.GetBinaryForm($bytes, 0)
    return $bytes
}

function Open-Policy([int] $AccessMask) {
    $oa = New-Object StrataLsa.LSA_OBJECT_ATTRIBUTES
    $oa.Length = [System.Runtime.InteropServices.Marshal]::SizeOf($oa)
    $handle = [IntPtr]::Zero
    $status = $Native::LsaOpenPolicy([IntPtr]::Zero, [ref]$oa, $AccessMask, [ref]$handle)
    if ($status -ne 0) {
        throw "LsaOpenPolicy failed (NTSTATUS 0x$('{0:X8}' -f $status), Win32 $($Native::LsaNtStatusToWinError($status)))"
    }
    return $handle
}

# The rights this account holds, as plain strings; $null when they cannot be read.
#
# Two LSA quirks make "no rights" and "cannot read" different answers, and both are normal:
#   * An account with NO rights short-circuits to STATUS_OBJECT_NAME_NOT_FOUND, which surfaces as
#     Win32 error 2.  That is a definite "none".
#   * An account that HAS rights must be read back, and a non-elevated token is refused with
#     STATUS_ACCESS_DENIED (0xC0000022).  That is NOT "none" - it is "ask an elevated shell".
# Reporting the second as a failure would make /status break for exactly the people who just
# granted the privilege, so it returns $null and the caller says "unknown".
function Get-AccountRights([IntPtr] $Policy, [byte[]] $SidBytes) {
    $ptr = [IntPtr]::Zero
    $count = 0
    $status = $Native::LsaEnumerateAccountRights($Policy, $SidBytes, [ref]$ptr, [ref]$count)
    if ($status -ne 0) {
        # The comma matters, and so does the one on the final return.  A function that outputs an
        # empty array outputs NOTHING - the pipeline enumerates it and finds no items - so `return @()`
        # arrives at the caller as $null, which is this function's word for "cannot read".  The
        # unary comma wraps the array so the empty ARRAY is one item and survives.  Without it,
        # "this account holds nothing" is reported as "unknown", and /status can never advise /add.
        if ($Native::LsaNtStatusToWinError($status) -eq 2) { return ,@() }
        return $null
    }
    $rights = @()
    try {
        $size = [System.Runtime.InteropServices.Marshal]::SizeOf([type]'StrataLsa.LSA_UNICODE_STRING')
        for ($i = 0; $i -lt $count; $i++) {
            $at = [IntPtr]::Add($ptr, $i * $size)
            $us = [System.Runtime.InteropServices.Marshal]::PtrToStructure(
                $at, [type]'StrataLsa.LSA_UNICODE_STRING')
            $rights += [System.Runtime.InteropServices.Marshal]::PtrToStringUni($us.Buffer, $us.Length / 2)
        }
    } finally {
        if ($ptr -ne [IntPtr]::Zero) { [void]$Native::LsaFreeMemory($ptr) }
    }
    return ,$rights          # see the note above about the comma
}

# Turn SeLockMemoryPrivilege on in THIS process's token, the same way and for the same reason the
# engine does before it allocates (src\core\pinned.cu:71-83): the right being assigned to the account
# is not enough, the process must ask for it.  Nothing here needs admin - it only ever enables a
# privilege the token already carries, and silently does nothing if the token does not.
#
# Returns @{ Enabled = <bool>; NotHeld = <bool>; Error = <int> }:
#   * NotHeld - AdjustTokenPrivileges reported ERROR_NOT_ALL_ASSIGNED: the right is not in this
#     token.  That is the ONLY condition under which "sign out and back in" is the right advice,
#     because it is exactly the case where the policy says yes and the logon token says no.
#   * Enabled - the token has it now, so whatever VirtualAlloc says next is the real answer.
function Enable-LockPagesPrivilege {
    $result = @{ Enabled = $false; NotHeld = $false; Error = 0 }
    $token = [IntPtr]::Zero
    if (-not $Native::OpenProcessToken($Native::GetCurrentProcess(),
                                       ($TokenAdjustPrivileges -bor $TokenQuery), [ref]$token)) {
        $result.Error = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        return $result
    }
    try {
        $luid = New-Object StrataLsa.LUID
        if (-not $Native::LookupPrivilegeValueW($null, $RightName, [ref]$luid)) {
            $result.Error = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
            return $result
        }
        $tp = New-Object StrataLsa.TOKEN_PRIVILEGES
        $tp.PrivilegeCount = 1
        $tp.LuidLowPart = $luid.LowPart
        $tp.LuidHighPart = $luid.HighPart
        $tp.Attributes = $SePrivilegeEnabled
        [void]$Native::AdjustTokenPrivileges($token, $false, [ref]$tp, 0, [IntPtr]::Zero, [IntPtr]::Zero)
        # AdjustTokenPrivileges returns TRUE even when it assigned nothing - the error says which.
        # It must be read before any other call can overwrite it.
        $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($err -eq $ErrorNotAllAssigned) {
            $result.NotHeld = $true
            $result.Error = $err
        } elseif ($err -ne 0) {
            $result.Error = $err
        } else {
            $result.Enabled = $true
        }
    } finally {
        [void]$Native::CloseHandle($token)
    }
    return $result
}

# The engine's allocation, tried for real.  Returns the Win32 error, or 0 when it worked.
function Test-LargePageAlloc {
    $minimum = $Native::GetLargePageMinimum().ToUInt64()
    if ($minimum -eq 0) { return @{ Ok = $false; Error = -1; Minimum = 0; NotHeld = $false } }
    $priv = Enable-LockPagesPrivilege
    $size = [UIntPtr]::new($minimum)
    $p = $Native::VirtualAlloc([IntPtr]::Zero, $size, ($MemReserve -bor $MemCommit -bor $MemLargePages), $PageReadWrite)
    if ($p -ne [IntPtr]::Zero) {
        [void]$Native::VirtualFree($p, [UIntPtr]::Zero, $MemRelease)
        return @{ Ok = $true; Error = 0; Minimum = $minimum; NotHeld = $false }
    }
    return @{ Ok = $false; Error = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
              Minimum = $minimum; NotHeld = $priv.NotHeld }
}

# NOT named for the parameter $Error: that is a read-only automatic variable in PowerShell, so it
# cannot be used as a parameter name.
function Get-LargePageErrorText([int] $ErrorCode) {
    switch ($ErrorCode) {
        1314 { 'ERROR_PRIVILEGE_NOT_HELD - this account does not hold SeLockMemoryPrivilege, or has not signed out and back in since it was granted' }
        1450 { 'ERROR_NO_SYSTEM_RESOURCES - the large-page pool is exhausted; close memory-heavy programs and try again' }
        -1   { 'this Windows reports no large-page minimum' }
        default { "Win32 error $ErrorCode" }
    }
}

function Invoke-Elevated([string] $TheAction, [string] $Sid) {
    $exe = (Get-Process -Id $PID).Path
    # Not $args: that is an automatic variable, and assigning to it has side effects.
    $childArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", $TheAction)
    if ($Sid) { $childArgs += @('-AccountSid', $Sid) }
    Write-Host "  Asking for administrator rights (one UAC prompt) ..."
    try {
        $p = Start-Process -FilePath $exe -Verb RunAs -ArgumentList $childArgs -Wait -PassThru
    } catch {
        Write-Host ''
        Write-Host "  Elevation was refused, so nothing was changed."
        Write-Host "  Run this from an elevated PowerShell instead:"
        Write-Host "    $PSCommandPath /$TheAction"
        Write-Host ''
        exit 3
    }
    exit $p.ExitCode
}

# --- actions ------------------------------------------------------------------------------------

function Show-Status([string] $Sid) {
    $sidBytes = Get-SidBytes $Sid
    $policy = Open-Policy $PolicyLookupNames
    try {
        $rights = Get-AccountRights $policy $sidBytes
    } finally {
        [void]$Native::LsaClose($policy)
    }
    # $null means LSA refused to read the list (a non-elevated token cannot read a POPULATED list);
    # an empty array is a definite "none".  The two must not be shown as the same thing.
    $known = $null -ne $rights
    $assigned = $known -and ($rights -contains $RightName)

    $probe = Test-LargePageAlloc
    $minimumMb = if ($probe.Minimum -gt 0) { $probe.Minimum / 1MB } else { 0 }
    $name = (New-Object System.Security.Principal.SecurityIdentifier $Sid).Translate(
        [System.Security.Principal.NTAccount]).Value
    $assignedText = if (-not $known) { 'unknown (needs an elevated shell to read)' }
                    elseif ($assigned) { 'yes' } else { 'no' }
    # NotHeld is a different failure from every other one: the token simply does not carry the
    # privilege, so VirtualAlloc's 1314 says nothing about the pool, the size, or this PC.
    $usableText = if ($probe.Ok) { 'yes - this logon can allocate large pages' }
                  elseif ($probe.NotHeld) { "no - $RightName is not in this logon's token" }
                  else { "no - $(Get-LargePageErrorText $probe.Error)" }

    Write-Host ''
    Write-Host "  Account            $name"
    Write-Host "                     $Sid"
    Write-Host "  Large page size    $(if ($minimumMb -gt 0) { "$minimumMb MB" } else { 'none reported' })"
    Write-Host "  $RightName"
    Write-Host "    assigned         $assignedText"
    Write-Host "    usable now       $usableText"
    Write-Host ''

    if ($probe.Ok -and $known -and -not $assigned) {
        # The window between /remove and the next sign-out.  Both facts are true at once and the
        # output would otherwise read as a contradiction: the account's policy no longer grants it,
        # but this session's token was built before that and still carries it.
        Write-Host "  This logon still carries $RightName even though the account no longer holds it."
        Write-Host "  The privilege is put into the token at logon, so /remove takes effect when you"
        Write-Host "  sign out and back in: Strata logs 'large pages' until then, '4 KB pages' after."
    } elseif ($probe.Ok) {
        Write-Host "  Strata's next start should log 'large pages ($($probe.Minimum) B)' instead of"
        Write-Host "  'large pages refused ... using 4 KB pages'."
    } elseif ($probe.NotHeld -and $assigned) {
        Write-Host "  The privilege is assigned to the account but is not in this logon's token: sign"
        Write-Host "  out and back in (or reboot), then run /status again.  Restarting Strata alone"
        Write-Host "  will not do it - the token was built when you logged on."
    } elseif ($probe.NotHeld -and $known) {
        Write-Host "  This account does not hold $RightName.  Run"
        Write-Host "  '$([System.IO.Path]::GetFileName($PSCommandPath)) /add' to grant it, then sign"
        Write-Host "  out and back in."
    } elseif ($probe.NotHeld) {
        Write-Host "  This shell cannot read the assignment, so the two causes of the line above are"
        Write-Host "  not yet told apart: either the privilege was never granted, or this logon's token"
        Write-Host "  was built before it was.  Run this from an elevated PowerShell to tell them apart,"
        Write-Host "  or just sign out and back in and look again."
    } else {
        Write-Host "  The privilege IS in this logon's token, so this is a real allocation failure"
        Write-Host "  rather than a permissions one.  Error 1450 means the large-page pool is exhausted:"
        Write-Host "  the whole arena has to be free as one block, so close memory-heavy programs first."
    }
    Write-Host ''
    exit 0
}

function Add-Right([string] $Sid) {
    $sidBytes = Get-SidBytes $Sid
    $policy = Open-Policy $PolicyAllAccess
    try {
        $lsaString = New-LsaString $RightName
        try {
            $status = $Native::LsaAddAccountRights($policy, $sidBytes, @($lsaString), 1)
            if ($status -ne 0) {
                throw "LsaAddAccountRights failed (NTSTATUS 0x$('{0:X8}' -f $status), Win32 $($Native::LsaNtStatusToWinError($status)))"
            }
        } finally {
            [void][System.Runtime.InteropServices.Marshal]::FreeHGlobal($lsaString.Buffer)
        }
    } finally {
        [void]$Native::LsaClose($policy)
    }
    Write-Host ''
    Write-Host "  Granted $RightName to $Sid."
    Write-Host ''
    Write-Host "  NOW SIGN OUT AND BACK IN (or reboot).  The privilege is put into the logon token, so"
    Write-Host "  it cannot reach a session that is already running - restarting Strata alone would"
    Write-Host "  still log '4 KB pages' and look like a failure."
    Write-Host ''
    Write-Host "  After you are back:  $([System.IO.Path]::GetFileName($PSCommandPath)) /status"
    Write-Host "  To undo it:          $([System.IO.Path]::GetFileName($PSCommandPath)) /remove"
    Write-Host ''
    exit 0
}

function Remove-Right([string] $Sid) {
    $sidBytes = Get-SidBytes $Sid
    $policy = Open-Policy $PolicyAllAccess
    try {
        $lsaString = New-LsaString $RightName
        try {
            $status = $Native::LsaRemoveAccountRights($policy, $sidBytes, $false, @($lsaString), 1)
            if ($status -ne 0) {
                throw "LsaRemoveAccountRights failed (NTSTATUS 0x$('{0:X8}' -f $status), Win32 $($Native::LsaNtStatusToWinError($status)))"
            }
        } finally {
            [void][System.Runtime.InteropServices.Marshal]::FreeHGlobal($lsaString.Buffer)
        }
    } finally {
        [void]$Native::LsaClose($policy)
    }
    Write-Host ''
    Write-Host "  Removed $RightName from $Sid."
    Write-Host "  Strata goes back to 4 KB pages on the next start after you sign out and back in."
    Write-Host ''
    exit 0
}

function Show-Help {
    Write-Host ''
    Write-Host '  Strata_LargePages.ps1 - 2 MB pages for the expert arena'
    Write-Host ''
    Write-Host '    Strata_LargePages.ps1 /status    say whether this PC can back the arena with large pages'
    Write-Host '    Strata_LargePages.ps1 /add       grant the privilege to this account (needs admin)'
    Write-Host '    Strata_LargePages.ps1 /remove    take it away again'
    Write-Host '    Strata_LargePages.ps1 /?         this text'
    Write-Host ''
    Write-Host '  A sign-out and sign-in is required after /add or /remove.  See the comments at the top'
    Write-Host '  of this file for why, and what the engine logs when it works.'
    Write-Host ''
    exit 0
}

# --- dispatch -----------------------------------------------------------------------------------

$normalized = $Action.TrimStart('/', '-').ToLowerInvariant()
if (-not $AccountSid) { $AccountSid = Get-CurrentSid }

switch ($normalized) {
    { $_ -in 'status', 'st', 's' } {
        Show-Status $AccountSid
    }
    { $_ -in 'add', 'grant', 'on' } {
        if (-not (Test-Elevated)) { Invoke-Elevated 'add' $AccountSid }
        Add-Right $AccountSid
    }
    { $_ -in 'remove', 'revoke', 'off' } {
        if (-not (Test-Elevated)) { Invoke-Elevated 'remove' $AccountSid }
        Remove-Right $AccountSid
    }
    { $_ -in '?', 'h', 'help' } {
        Show-Help
    }
    default {
        Write-Host ''
        Write-Host "  '$Action' is not an option this script knows."
        Write-Host "  Try /status, /add, /remove or /?."
        Write-Host ''
        exit 2
    }
}
