#!/bin/sh
# Arch's shipped wireplumber ALSA monitor hardcodes api.acp.auto-profile/auto-port
# to false (see ~/.config/wireplumber/wireplumber.conf.d/51-alsa-auto-profile.conf
# for the general fix). Even with that override, WirePlumber's built-in
# duplex-profile availability scoring for this codec (Realtek ALC289, no
# distinct internal-speaker port in its UCM profile) picks the mic-only
# profile instead of full duplex, and its default-nodes resolution can also
# fail to promote the sink from "configured" to actually "default". Force
# both explicitly on every login.

CARD=alsa_card.pci-0000_00_1f.3
SINK_NAME=alsa_output.pci-0000_00_1f.3.analog-stereo
SOURCE_NAME=alsa_input.pci-0000_00_1f.3.analog-stereo

for i in $(seq 1 20); do
    if pactl set-card-profile "$CARD" output:analog-stereo+input:analog-stereo 2>/dev/null; then
        break
    fi
    sleep 1
done

sleep 1

pw-metadata -n default 0 default.audio.sink "{\"name\":\"$SINK_NAME\"}" Spa:String:JSON >/dev/null 2>&1
pw-metadata -n default 0 default.configured.audio.sink "{\"name\":\"$SINK_NAME\"}" Spa:String:JSON >/dev/null 2>&1
pw-metadata -n default 0 default.audio.source "{\"name\":\"$SOURCE_NAME\"}" Spa:String:JSON >/dev/null 2>&1
pw-metadata -n default 0 default.configured.audio.source "{\"name\":\"$SOURCE_NAME\"}" Spa:String:JSON >/dev/null 2>&1
