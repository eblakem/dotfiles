if status is-interactive
    # Commands to run in interactive sessions can go here
end

source ~/.profile

/usr/bin/mise activate fish | source


# Added by Antigravity CLI installer
set -gx PATH "/home/michael/.local/bin" $PATH
