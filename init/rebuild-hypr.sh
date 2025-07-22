#/bin/sh 


yay -Qq | grep -E '(.+?-git)$' | yay -S --rebuild --noconfirm -


