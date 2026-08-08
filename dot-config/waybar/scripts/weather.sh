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

# Fall back to IP-based geolocation so the 7-day forecast still has coordinates
if [[ -z "$LAT" || -z "$LON" ]]; then
    IP_GEO="$(curl -s http://ip-api.com/json)"
    LAT="$(jq -r ".lat" <<< "$IP_GEO")"
    LON="$(jq -r ".lon" <<< "$IP_GEO")"
fi

text="$(curl -s "https://wttr.in/$LOC?format=1" | sed 's/ //g')"
tooltip="$(curl -s "https://wttr.in/$LOC?0QT" |
    sed 's/\\/\\\\/g' |
    sed ':a;N;$!ba;s/\n/\\n/g' |
    sed 's/"/\\"/g')"

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

forecast=""
if [[ -n "$LAT" && "$LAT" != "null" && -n "$LON" && "$LON" != "null" ]]; then
    CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}"
    CACHE_FILE="$CACHE_DIR/waybar-weather-forecast.json"
    CACHE_MAX_AGE=1800 # only refetch the forecast every 30 minutes

    mkdir -p "$CACHE_DIR"
    CACHE_AGE=99999
    if [[ -f "$CACHE_FILE" ]]; then
        CACHE_AGE=$(( $(date +%s) - $(stat -c %Y "$CACHE_FILE") ))
    fi

    if [[ "$CACHE_AGE" -lt "$CACHE_MAX_AGE" ]]; then
        FORECAST_JSON="$(cat "$CACHE_FILE")"
    else
        FORECAST_JSON="$(curl -s "https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&daily=weathercode,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=7")"
        [[ -n "$FORECAST_JSON" ]] && echo "$FORECAST_JSON" > "$CACHE_FILE"
    fi
    FORECAST_ROWS="$(jq -r '.daily.time as $t | .daily.weathercode as $c | .daily.temperature_2m_max as $mx | .daily.temperature_2m_min as $mn | range(0; ($t|length)) as $i | "\($t[$i])|\($c[$i])|\($mx[$i])|\($mn[$i])"' <<< "$FORECAST_JSON" 2>/dev/null)"

    while IFS='|' read -r day code tmax tmin; do
        [[ -z "$day" ]] && continue
        dow="$(date -d "$day" +%a 2>/dev/null || echo "$day")"
        icon="$(weather_icon "$code")"
        line="$(printf '%-3s %s  %3s° / %3s°' "$dow" "$icon" "$tmax" "$tmin")"
        forecast="${forecast}${line}\\n"
    done <<< "$FORECAST_ROWS"
fi

if ! grep -q "Unknown location" <<< "$text"; then
    if [[ -n "$forecast" ]]; then
        tooltip="${tooltip}\\n\\n<b>7-Day Forecast</b>\\n${forecast}"
    fi
    echo "{\"text\": \"$text\", \"tooltip\": \"<tt>$tooltip</tt>\", \"class\": \"weather\"}"
fi
