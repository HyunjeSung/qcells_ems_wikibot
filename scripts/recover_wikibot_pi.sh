#!/usr/bin/env bash
# 위키봇 Pi(wikibot-pi, 192.168.10.15) 접속 장애 복구 확인 스크립트.
# Wi-Fi 동글을 뺐다 끼우거나 Pi가 응답 없을 때: 먼저 Pi 전원을 뽑았다 다시 꽂은 뒤 이 스크립트 실행.
# 사용법: ./recover_wikibot_pi.sh
set -uo pipefail

PI_ETH_HOST="wikibot-pi"   # ~/.ssh/config: Hostname 192.168.10.15 (유선 eth0, Wi-Fi 상태와 무관하게 항상 연결됨)
TEAM_PORT=8010
MAX_WAIT_SEC=180
INTERVAL=5

echo "[1/4] Pi(eth0) 응답 대기 중... (최대 ${MAX_WAIT_SEC}초)"
elapsed=0
until ssh -o ConnectTimeout=3 -o BatchMode=yes "$PI_ETH_HOST" true 2>/dev/null; do
  if [ "$elapsed" -ge "$MAX_WAIT_SEC" ]; then
    echo "FAIL: ${MAX_WAIT_SEC}초 동안 eth0 응답 없음."
    echo "  -> Pi 전원 케이블/LED부터 확인하세요 (녹색 LED 점멸=정상 부팅 중). 아직 전원을 재시작 안 했다면 지금 하고 다시 실행하세요."
    exit 1
  fi
  sleep "$INTERVAL"
  elapsed=$((elapsed + INTERVAL))
done
echo "OK: eth0 응답 확인 (${elapsed}초 소요)"

echo "[2/4] wlan0(Qcells_Public) 상태 확인 중..."
WLAN_IP=$(ssh "$PI_ETH_HOST" "ip -4 -o addr show wlan0 2>/dev/null | awk '{print \$4}' | cut -d/ -f1")
if [ -z "$WLAN_IP" ]; then
  echo "  wlan0에 IP 없음 -> Qcells_Public 재연결 시도"
  ssh "$PI_ETH_HOST" "nmcli connection down Qcells_Public >/dev/null 2>&1; nmcli connection up Qcells_Public >/dev/null 2>&1"
  sleep 8
  WLAN_IP=$(ssh "$PI_ETH_HOST" "ip -4 -o addr show wlan0 2>/dev/null | awk '{print \$4}' | cut -d/ -f1")
fi
if [ -z "$WLAN_IP" ]; then
  echo "FAIL: wlan0 재연결 실패. 수동 점검 필요: ssh $PI_ETH_HOST 'nmcli device status'"
  exit 1
fi
echo "OK: wlan0 IP = $WLAN_IP"

echo "[3/4] 위키봇 서비스(qcells-wikibot.service) 상태 확인 중..."
SVC_STATE=$(ssh "$PI_ETH_HOST" "systemctl is-active qcells-wikibot.service")
if [ "$SVC_STATE" != "active" ]; then
  echo "  서비스 상태=$SVC_STATE -> 재시작 시도"
  ssh "$PI_ETH_HOST" "sudo systemctl restart qcells-wikibot.service"
  sleep 5
  SVC_STATE=$(ssh "$PI_ETH_HOST" "systemctl is-active qcells-wikibot.service")
fi
echo "서비스 상태: $SVC_STATE"

echo "[4/4] HTTP 헬스체크 (http://${WLAN_IP}:${TEAM_PORT}/health)..."
HTTP_CODE=$(ssh "$PI_ETH_HOST" "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://${WLAN_IP}:${TEAM_PORT}/health")
echo ""
if [ "$HTTP_CODE" = "200" ]; then
  echo "=== 복구 완료 ==="
  echo "팀 접속 주소: http://${WLAN_IP}:${TEAM_PORT}"
  if [ "$WLAN_IP" != "172.23.7.216" ]; then
    echo "※ 주소가 이전(172.23.7.216)과 다릅니다 — 게스트망 DHCP라 IP가 바뀔 수 있으니 팀에 새 주소를 공지하세요."
  fi
else
  echo "FAIL: /health가 HTTP ${HTTP_CODE} 반환. 수동 점검:"
  echo "  ssh $PI_ETH_HOST journalctl -u qcells-wikibot.service -n 50 --no-pager"
  exit 1
fi
