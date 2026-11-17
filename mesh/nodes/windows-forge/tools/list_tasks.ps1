# list_tasks.ps1 - survey the ai-forge scheduled tasks: state + what each runs.
# (Inline PS is $-stripped by the agent harness; scripts are the safe channel.)
$tasks = Get-ScheduledTask | Where-Object { $_.TaskName -like "ai-forge*" }
foreach ($t in $tasks) {
    Write-Output ("TASK: " + $t.TaskName + "  [" + $t.State + "]")
    foreach ($a in $t.Actions) { Write-Output ("  -> " + $a.Arguments) }
}
