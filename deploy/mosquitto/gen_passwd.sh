#!/bin/sh
# Mosquitto のパスワードファイル（deploy/mosquitto/passwd）を作る。リポジトリには入れない。
#
#   IOTSPEC_MQTT_PASSWORD=... deploy/mosquitto/gen_passwd.sh
#
# ユーザー名は device_id（mcu-c-01・mcu-cpp-01・gw-01・app-01）と observer。
# パスワードはすべて IOTSPEC_MQTT_PASSWORD（未設定なら開発用の既定値 iotspec-dev）。
# mosquitto_passwd が手元になければ、eclipse-mosquitto:2 のコンテナで実行する。
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
OUT="$DIR/passwd"
PASSWORD="${IOTSPEC_MQTT_PASSWORD:-iotspec-dev}"
USERS="mcu-c-01 mcu-cpp-01 gw-01 app-01 observer"

: > "$OUT"
if command -v mosquitto_passwd >/dev/null 2>&1; then
  for user in $USERS; do
    mosquitto_passwd -b "$OUT" "$user" "$PASSWORD"
  done
else
  docker run --rm --user "$(id -u):$(id -g)" -v "$DIR:/work" eclipse-mosquitto:2 \
    sh -c "for u in $USERS; do mosquitto_passwd -b /work/passwd \"\$u\" '$PASSWORD'; done"
fi
chmod 0644 "$OUT"
echo "作成: ${OUT}（ユーザー: ${USERS}）"
