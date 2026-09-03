#!/bin/sh
BSSIDS="$(nmcli device wifi list |
    awk 'NR>1 {if ($1 != "*") {print $1}}' |
    tr -d ":" |
    tr "\n" ",")"

LOC=""
REQUEST_GEO="$(wget -qO - http://openwifi.su/api/v1/bssids/"$BSSIDS")"
if [[ "$(jq ".count_results" <<< "$REQUEST_GEO")" -gt 0 ]] ; then
    LAT="$(jq ".lat" <<< "$REQUEST_GEO")"
    LON="$(jq ".lon" <<< "$REQUEST_GEO")"
    LOC="$LAT,$LON"
fi

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}"
LOCATION_CACHE="$CACHE_DIR/waybar-weather-location.json"
VPN_ACTIVE=""
nmcli -t -f TYPE connection show --active 2>/dev/null | grep -q "^wireguard$" && VPN_ACTIVE=1

# Cloudflare blocks openwifi.su for VPN exit IPs, so a VPN-active BSSID-lookup
# failure isn't "no data" - it's expected. Falling back to IP geolocation in
# that case reports the VPN exit's location, not ours, so reuse the last
# known-good fix instead.
if [[ -z "$LAT" || -z "$LON" ]]; then
    if [[ -n "$VPN_ACTIVE" && -f "$LOCATION_CACHE" ]]; then
        LAT="$(jq -r ".lat" <<< "$(cat "$LOCATION_CACHE")")"
        LON="$(jq -r ".lon" <<< "$(cat "$LOCATION_CACHE")")"
    else
        IP_GEO="$(curl -s http://ip-api.com/json)"
        LAT="$(jq -r ".lat" <<< "$IP_GEO")"
        LON="$(jq -r ".lon" <<< "$IP_GEO")"
    fi
fi

if [[ -n "$LAT" && "$LAT" != "null" && -n "$LON" && "$LON" != "null" && -z "$VPN_ACTIVE" ]]; then
    mkdir -p "$CACHE_DIR"
    printf '{"lat":%s,"lon":%s}' "$LAT" "$LON" > "$LOCATION_CACHE"
fi

weather_icon() {
    case "$1" in
        0) echo "☀️" ;;
        1|2) echo "🌤️" ;;
        3) echo "☁️" ;;
        45|48) echo "🌫️" ;;
        51|53|55|56|57) echo "🌦️" ;;
        61|63|65|66|67) echo "🌧️" ;;
        71|73|75|77) echo "🌨️" ;;
        80|81|82) echo "🌦️" ;;
        85|86) echo "🌨️" ;;
        95|96|99) echo "⛈️" ;;
        *) echo "❔" ;;
    esac
}

text=""
tooltip=""
forecast=""
if [[ -n "$LAT" && "$LAT" != "null" && -n "$LON" && "$LON" != "null" ]]; then
    CACHE_FILE="$CACHE_DIR/waybar-weather-forecast-${LAT}-${LON}.json"
    CACHE_MAX_AGE=1800 # only refetch every 30 minutes

    mkdir -p "$CACHE_DIR"
    CACHE_AGE=99999
    if [[ -f "$CACHE_FILE" ]]; then
        CACHE_AGE=$(( $(date +%s) - $(stat -c %Y "$CACHE_FILE") ))
    fi

    if [[ "$CACHE_AGE" -lt "$CACHE_MAX_AGE" ]]; then
        FORECAST_JSON="$(cat "$CACHE_FILE")"
    else
        FORECAST_JSON="$(curl -s "https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&current=temperature_2m,weathercode,windspeed_10m,cloudcover,shortwave_radiation&daily=weathercode,temperature_2m_max,temperature_2m_min,cloudcover_mean,shortwave_radiation_sum&timezone=auto&forecast_days=7")"
        [[ -n "$FORECAST_JSON" ]] && echo "$FORECAST_JSON" > "$CACHE_FILE"
    fi

    CUR_TEMP="$(jq -r '.current.temperature_2m' <<< "$FORECAST_JSON" 2>/dev/null)"
    CUR_CODE="$(jq -r '.current.weathercode' <<< "$FORECAST_JSON" 2>/dev/null)"
    CUR_WIND="$(jq -r '.current.windspeed_10m' <<< "$FORECAST_JSON" 2>/dev/null)"
    CUR_CLOUD="$(jq -r '.current.cloudcover' <<< "$FORECAST_JSON" 2>/dev/null)"
    CUR_RAD="$(jq -r '.current.shortwave_radiation' <<< "$FORECAST_JSON" 2>/dev/null)"
    TODAY_TMAX="$(jq -r '.daily.temperature_2m_max[0]' <<< "$FORECAST_JSON" 2>/dev/null)"
    TODAY_TMIN="$(jq -r '.daily.temperature_2m_min[0]' <<< "$FORECAST_JSON" 2>/dev/null)"

    if [[ -n "$CUR_TEMP" && "$CUR_TEMP" != "null" ]]; then
        CUR_ICON="$(weather_icon "$CUR_CODE")"
        text="${CUR_ICON} ${CUR_TEMP}°C"
        tooltip="<b>Now</b>\\n${CUR_ICON} ${CUR_TEMP}°C  Wind: ${CUR_WIND} km/h  Cloud: ${CUR_CLOUD}%  Solar: ${CUR_RAD} W/m²"
        if [[ -n "$TODAY_TMAX" && "$TODAY_TMAX" != "null" ]]; then
            tooltip="${tooltip}\\nToday: ${TODAY_TMIN}° / ${TODAY_TMAX}°"
        fi
    fi

    FORECAST_ROWS="$(jq -r '.daily.time as $t | .daily.weathercode as $c | .daily.temperature_2m_max as $mx | .daily.temperature_2m_min as $mn | .daily.cloudcover_mean as $cl | .daily.shortwave_radiation_sum as $rad | range(0; ($t|length)) as $i | "\($t[$i])|\($c[$i])|\($mx[$i])|\($mn[$i])|\($cl[$i])|\($rad[$i])"' <<< "$FORECAST_JSON" 2>/dev/null)"

    while IFS='|' read -r day code tmax tmin cloud rad; do
        [[ -z "$day" ]] && continue
        dow="$(date -d "$day" +%a 2>/dev/null || echo "$day")"
        icon="$(weather_icon "$code")"
        line="$(printf '%-3s %s  %3s° / %3s°  %3s%% cloud  %5s MJ/m²' "$dow" "$icon" "$tmax" "$tmin" "$cloud" "$rad")"
        forecast="${forecast}${line}\\n"
    done <<< "$FORECAST_ROWS"
fi

if [[ -n "$text" ]]; then
    if [[ -n "$forecast" ]]; then
        tooltip="${tooltip}\\n\\n<b>7-Day Forecast</b>\\n${forecast}"
    fi
    echo "{\"text\": \"$text\", \"tooltip\": \"<tt>$tooltip</tt>\", \"class\": \"weather\"}"
fi
