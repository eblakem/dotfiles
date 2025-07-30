#!/bin/bash

screenshot_dir=~/Screenshots

niri msg action screenshot &&
  inotifywait -e close $screenshot_dir &&
  swappy start --file "$(ls -d -t $screenshot_dir/* | head -n 1)"
