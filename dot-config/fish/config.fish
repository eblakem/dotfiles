if status is-interactive
    # Commands to run in interactive sessions can go here
end

source ~/.profile

/usr/bin/mise activate fish | source


# Added by Antigravity CLI installer
set -gx PATH "/home/michael/.local/bin" $PATH

# Pi
fish_add_path "/home/michael/.local/share/mise/installs/node/26.0.0/bin"

# mermaid-cli / mermaid-filter: use system chromium instead of downloading one
set -gx PUPPETEER_EXECUTABLE_PATH /usr/bin/chromium
