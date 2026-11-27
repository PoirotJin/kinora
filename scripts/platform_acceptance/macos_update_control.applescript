on run argv
    set appPID to (item 1 of argv) as integer
    set wanted to item 2 of argv
    set pressButton to (item 3 of argv) is "click"
    tell application "System Events"
        if not UI elements enabled then return "permission"
        set matches to application processes whose unix id is appPID
        if (count of matches) is 0 then return "missing"
        tell item 1 of matches
            if (count of windows) is 0 then return "missing"
            if pressButton then set frontmost to true
            set itemsInWindow to entire contents of window 1
            repeat with uiItem in itemsInWindow
                try
                    if role of uiItem is "AXButton" and name of uiItem is wanted then
                        if not enabled of uiItem then return "disabled"
                        if pressButton then perform action "AXPress" of uiItem
                        return "ready"
                    end if
                end try
            end repeat
        end tell
    end tell
    return "missing"
end run
