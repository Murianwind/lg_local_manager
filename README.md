# LG Local Manager

내 집 네트워크의 LG ThinQ 기기를 [rethink](https://github.com/anszom/rethink)로 로컬 제어하면서
공식 앱·구글홈도 그대로 쓸 수 있게 해주는 Windows 트레이 프로그램입니다.

DNAT를 지원하지 않는 공유기(IPTIME 순정 등)에서도, PC 하나로 다음 세 가지를 대신합니다:

1. **ARP 스푸핑** — 등록한 기기의 트래픽만 내 PC를 거치게 함 (Npcap + scapy)
2. **목적지 재작성(DNAT 역할)** — 443/8883 포트를 로컬 rethink 서버로 돌림 (WinDivert)
3. **rethink-cloud 실행/감시** — Node.js 서브프로세스로 기동, 죽으면 자동 재시작

기기 등록/삭제, 업데이트 확인/설치, 자동 시작 설정까지 전부 **로컬 웹 페이지 하나**
(`http://127.0.0.1:44490/`)에서 이루어집니다. 트레이 아이콘을 왼쪽 클릭하면 그 페이지가
열립니다 — rethink 자체도 같은 방식(로컬 웹 UI)으로 동작하니 통일한 셈입니다.

## ⚠️ 먼저 읽어주세요

- **자신이 소유/관리하는 홈 네트워크와, 자신이 등록한 자신의 기기에 한해서만 사용하세요.**
  ARP 스푸핑은 원래 다른 사람의 네트워크에 사용하면 안 되는 기술입니다. 이 프로그램은
  `devices.json`에 명시적으로 등록된 기기만 대상으로 삼도록 만들어져 있지만, 그 등록 자체를
  자신의 소유가 아닌 기기에 대해 하지 마세요.
- **GPL-2.0 안내**: rethink는 GPL-2.0으로 배포됩니다. rethink 소스는 `rethink-vendor.zip`에
  그대로 벤더링(고정)되어 있고(파일 개수가 많아 GitHub 웹 업로드 제한 때문에 zip 하나로
  묶어둠), 원본 라이선스 전문(`COPYING`)과 출처/커밋 정보(`VENDORED_FROM.md`)도 그 zip
  안에 함께 들어 있습니다. 이 프로젝트는 rethink를 수정 없이 그대로 서브프로세스로 실행할
  뿐이지만, 배포 zip에 rethink 빌드 결과물이 포함되므로 rethink 자체의 라이선스 조건
  (소스 공개 의무 등)은 그대로 적용됩니다.
- 되돌릴 때(기기 삭제/비활성화)는 rethink의 bridge가 먼저 꺼져야 합니다 — 순서를 지키지
  않으면 clientId 충돌로 재접속 루프에 빠질 수 있습니다. **기기에 rethink Device ID를
  등록해뒀다면 웹 UI가 자동으로 처리**하고, 등록 안 했다면 rethink 웹 UI에서 직접 꺼야 한다는
  안내가 뜹니다. 자세한 건 아래 "devices.json 형식" 참고.
- 라우터가 DNAT를 지원한다면(OpenWrt, ASUS 순정 등) **이 프로그램 없이 라우터 설정만으로 하는
  편이 훨씬 안정적**입니다. 이 프로그램은 그게 불가능한 경우를 위한 대안입니다.

## 요구 사항

- Windows 10/11 (관리자 권한 실행)
- [Npcap](https://npcap.com/#download) — 설치 시 **"WinPcap API-compatible Mode"** 체크
  (raw 패킷 캡처/전송용 드라이버. 자동 설치는 하지 않으므로 최초 1회 직접 설치해야 합니다)
- WinDivert.dll / WinDivert64.sys — release zip에 이미 포함되어 있어 별도 설치 불필요
- `openssl.exe` — rethink-cloud가 인증서 발급에 커맨드로 직접 사용합니다. **release zip에
  이미 번들되어 있어 별도 설치가 필요 없습니다** (Git for Windows의 `usr/bin`+`usr/ssl`을
  그대로 가져와 `runtime/openssl/`에 포함시켰습니다 — 출처는 GPL-2.0, `.gitignore`로 빌드
  시에만 받아지므로 저장소에는 없습니다). 번들된 사본을 최우선으로 쓰고, 없을 경우에만
  시스템 PATH나 별도 설치된 Git for Windows를 찾아봅니다.

## 설치 및 실행

1. [Releases](../../releases) 에서 **`LGLocalManager-Full-*-win-x64.zip`**(처음 설치용, 전체 패키지)
   다운로드 후 압축 해제
2. Npcap 설치 (아직 안 했다면)
3. `config/settings.example.json` → `config/settings.json` 으로 복사, 게이트웨이 IP 등 입력
4. `LGLocalManager.exe` 실행 (관리자 권한 요청 창이 뜨면 승인)
5. **`config/rethink-config.json`이 없으면 자동으로 웹 페이지가 브라우저에 뜹니다**
   (`http://127.0.0.1:44490/`) — MQTT 브로커 주소만 입력하면 나머지 값(포트, hostname 등)은
   자동으로 채워지고, 제출하는 즉시 rethink-cloud가 시작됩니다. (스키마는
   `rethink-vendor.zip` 안의 `config.jsonc` 원본을 그대로 따르며, 우리가 vendor로 고정해둔
   [PR #107](https://github.com/anszom/rethink/pull/107) 버전의 기능이 포함되어 있습니다.)
6. 설정이 끝나면 같은 페이지가 **대시보드**로 바뀝니다 — 여기서 이름/MAC/IP를 입력해 기기를
   추가합니다
7. rethink 웹 UI(대시보드 상단 링크)에서 기기가 올라왔는지 확인 → bridge 활성화

## 웹 대시보드 (`http://127.0.0.1:44490/`)

트레이 아이콘을 **왼쪽 클릭**하면 이 페이지가 열립니다 (우클릭하면 "대시보드 열기"/"종료"
두 항목만 있는 메뉴가 뜹니다 — 나머지 조작은 전부 이 페이지 안에서 이루어집니다).

같은 주소, 같은 서버가 상태에 따라 다르게 그립니다:

- **`rethink-config.json`이 아직 없거나 MQTT 브로커 주소가 비어 있으면** → 최초 설정 폼
  (MQTT 브로커 주소 입력 + "연결 테스트" 버튼으로 TCP 도달 여부 바로 확인 가능). 제출하면
  즉시 rethink-cloud가 시작되고 페이지가 대시보드로 전환됩니다.
- **설정이 끝났으면** → 대시보드:
  - **기기 관리** — 표에서 기기 추가/활성·비활성 전환/rethink Device ID 설정/삭제
  - **업데이트** — Stable/Beta 채널 선택, "지금 확인" 버튼, 새 버전 있으면 "지금 설치" 버튼
  - **시작 옵션** — Windows 시작 시 자동 실행 체크박스
  - **로그** — 최근 150줄을 페이지 안에서 바로 확인 (별도로 로그 파일을 열 필요 없음)

폼 제출은 전부 POST → 303 리다이렉트 → GET / 방식이라 자바스크립트 없이도 동작하고,
새로고침해도 중복 제출이 안 됩니다.

## Windows 시작 시 자동 실행

대시보드의 **"Windows 시작 시 자동 실행"** 체크박스로 켜고 끕니다. 이 앱은 관리자 권한이
필요해서(WinDivert/ARP 스푸핑), 일반적인 "시작 프로그램 폴더" 방식은 부팅마다 UAC 승인 창이
뜹니다. 대신 내부적으로 **작업 스케줄러(Task Scheduler)**에 "로그온 시 + 가장 높은 권한으로
실행" 트리거로 등록해서, UAC 프롬프트 없이 조용히 관리자 권한으로 자동 실행되게 합니다.
등록/해제 상태는 `schtasks /Query /TN LGLocalManager_AutoStart` 로 직접 확인할 수도 있습니다.

## 업데이트 확인 및 자동 설치

대시보드의 **"업데이트"** 섹션에서 채널을 고르고 "지금 확인"을 누르면 그 즉시 GitHub
Releases를 조회합니다(백그라운드 자동 주기 체크는 하지 않습니다).

**업데이트 채널**은 두 가지입니다:

- **Stable** — `release` 발행(정식 태그, `prerelease == false`)으로 만들어진 릴리즈만 인식합니다.
- **Beta** — `workflow_dispatch`로 수동 빌드한 것(타임스탬프 태그, `prerelease == true`)까지
  포함해 가장 최근 릴리즈를 인식합니다.

채널은 `config/settings.json`의 `update_channel` 값(`"stable"` 또는 `"beta"`)으로 저장되며,
채널을 바꾸면 이전에 감지해둔 업데이트 정보는 초기화됩니다(다시 확인해야 함).

새 버전이 확인되면 대시보드에 "지금 설치" 버튼이 나타납니다.

**Full 재설치가 필요한지는 앱이 자동으로 판단합니다.** 릴리즈마다 Node/rethink/WinDivert/
openssl 버전 지문이 담긴 작은 `runtime-manifest.json`이 함께 올라가는데, 앱은 이 파일만
먼저 조회해서 자기 폴더의 `runtime-manifest.json`과 비교합니다:

- **같음** → runtime은 안 바뀐 것 → **`LGLocalManager-Update-*.zip`**(exe만, 수 MB)만 받아서
  `LGLocalManager.exe` 하나만 교체
- **다름 / 로컬 파일 없음** → Node·rethink·WinDivert·openssl 중 뭔가 바뀐 것 → 안전하게
  **`LGLocalManager-Full-*.zip`**(전체 패키지)을 받아서, `config/`·`data/`(사용자 설정·기기
  목록·로그)는 보존하고 나머지 전체를 교체

대시보드에 어느 쪽으로 처리될지("간단 업데이트" / "전체 재설치")가 함께 표시됩니다.
어느 쪽이든 앱이 정상 종료 절차를 거쳐 스스로 꺼지고, 백그라운드 스크립트가 파일을 교체한 뒤
재실행하며, 다운로드에 썼던 임시 폴더를 스스로 정리합니다.

`app/updater.py`의 `GITHUB_REPO` 상수가 실제 저장소 이름(`owner/repo`)과 일치해야 합니다.

## devices.json 형식

```json
{
  "rethink": {
    "https_port": 443,
    "mqtts_port": 8883,
    "management_port": 44401
  },
  "devices": [
    {
      "name": "거실 에어컨",
      "mac": "AA:BB:CC:11:22:33",
      "ip": "192.168.0.101",
      "enabled": true,
      "rethink_device_id": ""
    }
  ]
}
```

파일을 텍스트 에디터로 직접 편집해도 앱이 변경을 감지해(2초 폴링) 자동으로 반영합니다.
`enabled: false` 로 바꾸면 리다이렉트/ARP 스푸핑만 멈추고 등록 정보는 남습니다(임시 중단).

### rethink_device_id (선택, bridge 자동 비활성화용)

기기가 rethink에 처음 연결되면, rethink 웹 UI(`http://127.0.0.1:44401/`)의
**"Connected devices"** 표에 그 기기의 **ID** 컬럼 값(LG가 부여한 UUID)이 나타납니다.
이 값을 대시보드의 기기 표에서 직접 입력해 저장해두면, 그 뒤로 이 기기를 비활성화하거나
삭제할 때 앱이 rethink의 관리 API(`POST /bridge/:deviceId/disable`)를 자동으로 호출해서
clientId 충돌 없이 안전하게 LG 공식 서버로 되돌립니다. 등록해두지 않으면 예전처럼
"rethink 웹 UI에서 직접 꺼주세요" 안내만 뜹니다 — MAC/IP만으로는 rethink 쪽 deviceId를
알아낼 방법이 없어서(rethink 관리 API 자체가 그 매핑을 제공하지 않음), 최초 한 번은
사용자가 직접 확인해 넣어야 합니다. 목록에서 완전히 지우면 원상복귀(LG 공식 서버로 복귀)
처리됩니다.

## 폴더 구조 (release zip 기준)

```
LGLocalManager.exe
WinDivert.dll
WinDivert64.sys
config/
  settings.json           (직접 생성)
  rethink-config.json      (웹 UI에서 자동 생성)
data/
  devices.json             (최초 실행 시 자동 생성)
  lglocalmanager.log
runtime/
  node/                     (포터블 Node.js, 빌드가 자동 포함)
  rethink/dist/             (빌드된 rethink-cloud)
  rethink/node_modules/
  openssl/bin/, openssl/ssl/ (Git for Windows에서 가져온 openssl.exe + 설정 파일)
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
Expand-Archive -Path rethink-vendor.zip -DestinationPath .
cd rethink-vendor
npm ci
npm run build
cd ..
# 이후 포터블 Node/WinDivert/openssl 배치는 .github/workflows/build.yml 의 단계를 참고해
# 수동으로 채워 넣어야 함
```

## 라이선스

이 저장소(LGLocalManager)의 코드는 각자 원하는 라이선스를 선택하세요(기본값 없음).
`rethink-vendor.zip`에 벤더링된 rethink는 GPL-2.0이며, 원본 저작권은
[anszom](https://github.com/anszom/rethink)에게 있습니다. 출처와 정확한 커밋은
그 zip 안의 `VENDORED_FROM.md`를 참고하세요.

빌드 시 `runtime/openssl/`에 번들되는 openssl.exe와 그 실행에 필요한 라이브러리/설정 파일은
[Git for Windows](https://github.com/git-for-windows/git)의 `usr/bin`, `usr/ssl`에서 그대로
가져온 것이며, GPL-2.0으로 배포됩니다.
