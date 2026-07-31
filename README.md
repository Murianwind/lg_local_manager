# LG Local Manager

내 집 네트워크의 LG ThinQ 기기를 [rethink](https://github.com/anszom/rethink)로 로컬 제어하면서
공식 앱·구글홈도 그대로 쓸 수 있게 해주는 Windows 트레이 프로그램입니다.

DNAT를 지원하지 않는 공유기(IPTIME 순정 등)에서도, PC 하나로 다음 세 가지를 대신합니다:

1. **ARP 스푸핑** — 등록한 기기의 트래픽만 내 PC를 거치게 함 (Npcap + scapy)
2. **목적지 재작성(DNAT 역할)** — 443/8883 포트를 로컬 rethink 서버로 돌림 (WinDivert)
3. **rethink-cloud 실행/감시** — Node.js 서브프로세스로 기동, 죽으면 자동 재시작

기기 등록/삭제는 `data/devices.json` 파일 하나로 관리하며, 트레이 메뉴의 "기기 관리" 창에서도
추가/삭제할 수 있습니다.

## ⚠️ 먼저 읽어주세요

- **자신이 소유/관리하는 홈 네트워크와, 자신이 등록한 자신의 기기에 한해서만 사용하세요.**
  ARP 스푸핑은 원래 다른 사람의 네트워크에 사용하면 안 되는 기술입니다. 이 프로그램은
  `devices.json`에 명시적으로 등록된 기기만 대상으로 삼도록 만들어져 있지만, 그 등록 자체를
  자신의 소유가 아닌 기기에 대해 하지 마세요.
- **GPL-2.0 안내**: rethink는 GPL-2.0으로 배포됩니다. 이 프로젝트는 rethink를 수정 없이
  서브프로세스로 실행할 뿐 소스를 변형해 재배포하지 않지만, 배포 zip에 rethink 빌드 결과물이
  포함되므로 rethink 자체의 라이선스 조건(소스 공개 의무 등)은 그대로 적용됩니다. `runtime/rethink`
  안의 rethink 소스/빌드 산출물을 임의로 수정해 재배포할 경우 GPL-2.0을 준수해야 합니다.
- 되돌릴 때(기기 삭제/비활성화)는 **rethink 웹 UI에서 해당 기기의 bridge를 먼저 끄고** 나서
  진행하세요. 순서를 지키지 않으면 clientId 충돌로 재접속 루프에 빠질 수 있습니다 — 앱이
  삭제/비활성화 시 이를 트레이 알림으로 안내합니다.
- 라우터가 DNAT를 지원한다면(OpenWrt, ASUS 순정 등) **이 프로그램 없이 라우터 설정만으로 하는
  편이 훨씬 안정적**입니다. 이 프로그램은 그게 불가능한 경우를 위한 대안입니다.

## 요구 사항

- Windows 10/11 (관리자 권한 실행)
- [Npcap](https://npcap.com/#download) — 설치 시 **"WinPcap API-compatible Mode"** 체크
  (raw 패킷 캡처/전송용 드라이버. 자동 설치는 하지 않으므로 최초 1회 직접 설치해야 합니다)
- WinDivert.dll / WinDivert64.sys — release zip에 이미 포함되어 있어 별도 설치 불필요

## 설치 및 실행

1. [Releases](../../releases) 에서 **`LGLocalManager-Full-*-win-x64.zip`**(처음 설치용, 전체 패키지)
   다운로드 후 압축 해제
2. Npcap 설치 (아직 안 했다면)
3. `config/settings.example.json` → `config/settings.json` 으로 복사, 게이트웨이 IP 등 입력
4. `config/rethink-config.example.json` → `config/rethink-config.json` 으로 복사,
   [rethink 원본 설정 문서](https://github.com/anszom/rethink/wiki)를 참고해 채우기
5. `LGLocalManager.exe` 실행 (관리자 권한 요청 창이 뜨면 승인)
6. 트레이 아이콘 우클릭 → **기기 관리** → 이름/MAC/IP 입력 후 추가
7. 잠시 뒤 트레이 → **rethink 웹 UI 열기** 에서 기기가 올라왔는지 확인 → bridge 활성화

## Windows 시작 시 자동 실행

트레이 메뉴의 **"Windows 시작 시 자동 실행"** 항목을 체크하면 됩니다. 이 앱은 관리자 권한이
필요해서(WinDivert/ARP 스푸핑), 일반적인 "시작 프로그램 폴더" 방식은 부팅마다 UAC 승인 창이
뜹니다. 대신 내부적으로 **작업 스케줄러(Task Scheduler)**에 "로그온 시 + 가장 높은 권한으로
실행" 트리거로 등록해서, UAC 프롬프트 없이 조용히 관리자 권한으로 자동 실행되게 합니다.
등록/해제 상태는 `schtasks /Query /TN LGLocalManager_AutoStart` 로 직접 확인할 수도 있습니다.

## 업데이트 확인 및 자동 설치

트레이 메뉴 상단에 **"현재 버전: vX.X.X (클릭하여 확인)"** 이 표시됩니다. 클릭하면 그 즉시
GitHub Releases를 조회해 새 버전이 있는지 확인합니다(백그라운드 자동 주기 체크는 하지 않습니다).

**업데이트 채널**은 두 가지입니다 (트레이 메뉴 → "업데이트 채널"):

- **Stable** — `release` 발행(정식 태그, `prerelease == false`)으로 만들어진 릴리즈만 인식합니다.
- **Beta** — `workflow_dispatch`로 수동 빌드한 것(타임스탬프 태그, `prerelease == true`)까지
  포함해 가장 최근 릴리즈를 인식합니다.

채널은 `config/settings.json`의 `update_channel` 값(`"stable"` 또는 `"beta"`)으로 저장되며,
채널을 바꾸면 이전에 감지해둔 업데이트 정보는 초기화됩니다(다시 클릭해서 확인해야 함).

새 버전이 확인되면 트레이 알림이 뜨고 메뉴에 **"업데이트 설치"** 항목이 나타납니다.

**Full 재설치가 필요한지는 앱이 자동으로 판단합니다.** 릴리즈마다 Node/rethink/WinDivert
버전 지문이 담긴 작은 `runtime-manifest.json`이 함께 올라가는데, 앱은 이 파일만 먼저
조회해서 자기 폴더의 `runtime-manifest.json`과 비교합니다:

- **같음** → runtime은 안 바뀐 것 → **`LGLocalManager-Update-*.zip`**(exe만, 수 MB)만 받아서
  `LGLocalManager.exe` 하나만 교체
- **다름 / 로컬 파일 없음** → Node·rethink·WinDivert 중 뭔가 바뀐 것 → 안전하게
  **`LGLocalManager-Full-*.zip`**(전체 패키지)을 받아서, `config/`·`data/`(사용자 설정·기기
  목록·로그)는 보존하고 나머지 전체를 교체

트레이 알림에 어느 쪽으로 처리될지("간단 업데이트" / "전체 재설치")가 함께 표시됩니다.
어느 쪽이든 앱이 정상 종료 절차를 거쳐 스스로 꺼지고, 백그라운드 스크립트가 파일을 교체한 뒤
재실행하며, 다운로드에 썼던 임시 폴더를 스스로 정리합니다.

`app/updater.py`의 `GITHUB_REPO` 상수가 실제 저장소 이름(`owner/repo`)과 일치해야 합니다.

## devices.json 형식

```json
{
  "rethink": {
    "https_port": 4433,
    "mqtt_port": 8883,
    "mgmt_port": 44401
  },
  "devices": [
    {
      "name": "거실 에어컨",
      "mac": "AA:BB:CC:11:22:33",
      "ip": "192.168.0.101",
      "enabled": true
    }
  ]
}
```

파일을 텍스트 에디터로 직접 편집해도 앱이 변경을 감지해(2초 폴링) 자동으로 반영합니다.
`enabled: false` 로 바꾸면 리다이렉트/ARP 스푸핑만 멈추고 등록 정보는 남습니다(임시 중단).
목록에서 완전히 지우면 원상복귀(LG 공식 서버로 복귀) 처리됩니다.

## 폴더 구조 (release zip 기준)

```
LGLocalManager.exe
WinDivert.dll
WinDivert64.sys
config/
  settings.json           (직접 생성)
  rethink-config.json      (직접 생성)
data/
  devices.json             (최초 실행 시 자동 생성)
  lglocalmanager.log
runtime/
  node/                     (포터블 Node.js, 빌드가 자동 포함)
  rethink/dist/             (빌드된 rethink-cloud)
  rethink/node_modules/
```

## 알려진 제약

- ThinQ1(구형 기기)은 대상이 아닙니다 — rethink 자체가 ThinQ2 기기 위주로 지원합니다.
- 대상 기기는 고정 IP(공유기 DHCP 예약 권장)여야 합니다. IP가 바뀌면 재등록이 필요합니다.
- ARP 스푸핑이 켜져 있는 동안 PC가 꺼지면(절전 포함) 대상 기기가 자동으로 LG 공식 서버로
  복귀합니다 — 이건 의도된 동작입니다(장애 시 안전하게 원상복귀).
- 라즈베리파이 등 상시 구동 가능한 리눅스 장비로 옮기고 싶다면, `arp_engine.py`/
  `redirect_engine.py` 의 로직을 각각 `scapy`(그대로 재사용 가능)와 `iptables` DNAT 규칙으로
  치환하면 됩니다 — 구조를 그렇게 나눠둔 이유이기도 합니다.

## 빌드 (로컬에서 직접 하고 싶다면)

CI(GitHub Actions)가 `git push --tags`로 태그를 올리면 자동으로 빌드/릴리즈를 생성합니다.
로컬 빌드가 필요 없다면 이 섹션은 건너뛰어도 됩니다.

```powershell
pip install -r requirements.txt
pyinstaller build.spec
# 이후 rethink/node 런타임은 .github/workflows/build.yml 의 단계를 참고해 수동으로 채워 넣어야 함
```

## 라이선스

이 저장소(LGLocalManager)의 코드는 각자 원하는 라이선스를 선택하세요(기본값 없음).
번들되는 rethink는 GPL-2.0이며, 원본 저작권은 [anszom](https://github.com/anszom/rethink)에게
있습니다.
