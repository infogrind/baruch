# The README's demo GIF, recorded from the demo sandbox (fake NAS, real
# rsync, real CLI). Re-record from the repo root after output changes:
#
#     vhs demo/demo.tape        # writes docs/demo.gif (needs `brew install vhs`)

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1300
Set Height 980
Set Padding 20
Set Theme "Builtin Dark"
Set TypingSpeed 60ms

Hide
Type "export PS1='$ ' BARUCH_DEMO=$(mktemp -d) && alias baruch=demo/baruch" Enter
Type "uv run demo/sandbox.py setup $BARUCH_DEMO >/dev/null" Enter
Type "uv run demo/sandbox.py dock $BARUCH_DEMO BK-20A && echo y | baruch run >/dev/null" Enter
Type "uv run demo/sandbox.py change $BARUCH_DEMO && clear" Enter
Sleep 8s
Show

Type "# BK-20A is docked; some files changed on the NAS since its last run" Enter
Sleep 1s
Type "baruch run" Sleep 500ms Enter
Sleep 4s
Type "y" Sleep 500ms Enter
Sleep 4s
Type "clear && baruch status" Sleep 500ms Enter
Sleep 5s
