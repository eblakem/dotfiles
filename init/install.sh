#!/bin/bash 

# setup yay
if command -v yay >&2; then
	echo "Yay installed, skipping"
else 
	sudo pacman -S --needed git base-devel
	git clone https://aur.archlinux.org/yay-bin.git
	cd yay-bin
	makepkg -si
    cd ..
fi

# create .profile if it doesn't exist, used by fish
touch ~/.profile

# enable yay to update -git packages when upgrading
yay -S --devel --save

# get latest updates
echo "Checking for system updates"
yay -Syu --noconfirm

# install default packages
echo "Installing default packages"
yay -S --needed --noconfirm - < pkglist.txt

# enable default services
echo "Enabling default services (bluetooth, greetd, cups, ufw, docker) if required"
systemctl is-active --quiet bluetooth.service || sudo systemctl enable bluetooth.service 
systemctl is-active --quiet greetd.service || sudo systemctl enable greetd.service
systemctl is-active --quiet cups.service || sudo systemctl enable cups.service
systemctl is-active --quiet ufw.service || sudo systemctl enable ufw.service
systemctl is-active --quiet docker.service || sudo systemctl enable docker.service

# ask user if they want to install work packages 
read -p "Install Work packages? (y/N) " workyn
[[ "$workyn" == [Yy]* ]] && yay -S --needed - < work-pkglist.txt
[[ "$workyn" == [Yy]* ]] && (systemctl is-active --quiet displaylink.service || sudo systemctl enable displaylink.service)

read -p "Install Home packages (y/N) " homeyn
[[ "$homeyn" == [Yy]* ]] && yay -S --needed - < home-pkglist.txt

read -p "Install PC packages (y/N) " pcyn
[[ "$pcyn" == [Yy]* ]] && yay -S --needed - < pc-pkglist.txt

# disable incoming connections
echo "Updating firewall to deny incoming connections"
sudo ufw enable
sudo ufw default deny incoming

# setup greetdtui
read -p "Overwrite greetd config file (y/N) " greetdyn
[[ "$greetdyn" == [Yy]* ]] && sudo cp ../etc/greetd/config.toml /etc/greetd/

# setup docker
echo "Adding user to Docker group so they can run as non-sudo"
cat /etc/group | grep docker > /dev/null 2>&1 || sudo groupadd docker
groups $USER | grep docker > /dev/null 2>&1 || sudo usermod -aG docker $USER

# setup devpods
read -p "Install devpod (y/N) " devpodyn
[[ "$devpodyn" == [Yy]* ]] && curl -L -o devpod "https://github.com/loft-sh/devpod/releases/latest/download/devpod-linux-amd64" && sudo install -c -m 0755 devpod /usr/local/bin && rm -f devpod

# setup ghcup
read -p "Install ghcup (y/N) " ghcupyn
[[ "$ghcupyn" == [Yy]* ]] && curl --proto '=https' --tlsv1.2 -sSf https://get-ghcup.haskell.org | sh

# add gemini api key
read -p "Add gemini api key environment variable (y/N) " gemyn
if [[ "$gemyn" == [Yy]* ]]; then 
    read -p "Enter gemini api key " gemapi
    echo "export GEMAPI=$gemapi" >> ~/.profile
fi 

# clean unneeded dependencies
yay -Yc

# setup configs
cd ..
./stow-adopt.sh


