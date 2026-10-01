# Jenkins Config

單一 `settings.local.yaml` → Jinja2 → Docker Compose 與 JCasC。
Config 只建立 seed；Job 定義由獨立 pipelines repo 管理。本專案只支援 Docker。

## 開始使用

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)、Python 3.12+、Docker Engine／Desktop 與 Compose v2。
uv 管理 Jinja2／PyYAML，版本由 `uv.lock` 固定；不需要 gomplate、jq、Helm 或 pip。

```bash
uv sync --locked
cp settings.example.yaml settings.local.yaml
```

只編輯 `settings.local.yaml`：依 `settings.example.yaml` 的 `images`、`jenkins`、`pipeline`、`docker`、`credentials` 區段填值。
`your-org` 必須換成自己的來源；映像使用明確版本或 digest，不能填 latest／main／字面 v。

```bash
./deploy.sh
```

`deploy.sh` 依序執行既有 Python CLI 的 init／up，uv 自動使用鎖定依賴。
可從其他目錄以完整路徑執行；選項中的相對路徑以本專案目錄為準。

```bash
./deploy.sh --settings settings.local.yaml --state .state --secrets .secrets
./deploy.sh --help
```

若只想查看生成配置，可執行 `uv run --locked python -m jenkins_config render`。

管理員密碼可在同一份 `settings.local.yaml` 設定：

```yaml
jenkins:
  admin:
    user: admin
    password: '填入你的密碼'
```

`jenkins.admin.password` 設為 `null` 時，沿用 `.secrets/admin_password`；首次初始化才隨機產生。
有指定時，`init` 用它建立缺少的秘密檔；`./deploy.sh`／`up` 會同步密碼並重建 controller 套用，保留資料 volume。
變更密碼請使用 `./deploy.sh`，`reload` 不處理密碼變更。
密碼不會放入生成的 Compose／JCasC 或命令列輸出。`settings.local.yaml` 已被 Git ignore；請勿提交含真實密碼的 YAML。

`init` 不覆寫既有秘密檔；部署時才套用 YAML 明確指定的密碼。
`up` 啟動 controller 並等待健康檢查、載入 JCasC、取得 inbound agent secret、啟動 agent，然後觸發 seed。

預設 http://localhost:18080/，帳號為 admin，密碼讀取上述秘密檔。
在 seed 的 Console Output 確認 SUCCESS，再查看 `pdd/integration-test`。

## 日常操作

| 指令尾端 | 行為 |
| --- | --- |
| `status` | 查看服務狀態 |
| `seed` | 同步流程 repo 的 Job DSL |
| `reload` | 重新渲染並載入 JCasC，再觸發 seed |
| `down` | 停止服務，保留 volumes |

指令入口皆為 `uv run --locked python -m jenkins_config`。
支援 `--settings FILE`、`--state DIR`、`--secrets DIR`；down／status 使用已渲染的 Compose，不讀取新設定。

產物為 `.state/compose.yaml`、`.state/casc/jenkins.yaml`，請勿手改。
升級映像後執行 up；更改 project 會改用另一組 volumes，原資料不會自動搬移。

## 憑證與執行環境

私人流程 repo 設 `credentials.pipeline_git.enabled: true`，在同區段填入 `username` 和 `token`；被測 repo 使用 `credentials.application_git`。部署時才從本機設定產生秘密檔。
目前支援 HTTP(S)，帶憑證須使用 HTTPS；`settings.local.yaml` 已被 Git ignore，勿提交真實 token。GHCR 使用本機 `docker login ghcr.io`。

Agent 使用 WebSocket，只有 controller 的 HTTP port 綁定本機；agent 不掛管理員密碼檔。
Docker socket 的群組填入 `docker.socket_gid`；Linux 可用 `stat -c %g /var/run/docker.sock` 查詢。

此部署執行受信任的流程；Docker socket 可控制 daemon，測試容器隔離不等於多租戶安全邊界。
秘密檔需可由容器 UID 1000 讀取；其他 host UID 需安排適當權限。

## Seed 與流程 repo

流程 repo 預設 `jobs/**/*.groovy`，seed 約每 5 分鐘透過 SCM polling 檢查更新。
Job DSL 可使用 PIPELINE_REPO、PIPELINE_BRANCH、PIPELINE_CREDENTIALS（只含 credential ID）。

Controller 映像須含 authorize-project；seed 以 `jenkins.admin.user` 身分在 sandbox 執行。
新增 Job 只改 pipelines repo；移除定義會停用 Job、保留歷史。其他 seed 的 Job 所有權衝突會失敗。

範例 Job 提供 success／unstable／failure 與 JUnit，仍是手動觸發。
cloth_shop PR／MR、SSH Git 與進階 GitLab 支援尚未實作，維持 REQ-003 的設計狀態。

## 升級既有部署

1. 備份 Jenkins home 與秘密，保留既有 volumes。
2. 依 `settings.example.yaml` 將舊平面欄位移入巢狀區段，移除 `kubernetes` 區段。
3. 更新 `images.controller` 為移除 K8s 外掛的新映像；`images.agent` 可沿用。
4. 將新的 Docker 專用 Jenkinsfile 同步到流程 repo，再執行 uv sync／up。

若仍使用更早的 `pipeline_path`，先改為 `pipeline.seed.dsl` 與 `pipeline.seed.poll`。
Jenkins home 中的既存外掛不會隨換映像刪除；備份後透過外掛管理移除 K8s 與無其他用途的相依，再重啟。

工作目錄中的舊 Helm chart、K8s 渲染產物與測試 fixture 已清除；本地設定已遷移至巢狀欄位。
`down` 保留資料；備份應在停止服務後進行，勿把 `down -v` 當日常操作。

## 程式結構與驗證

| 元件 | 責任 |
| --- | --- |
| Settings | 讀取並驗證單一 YAML |
| TemplateRenderer | 以 StrictUndefined 渲染 Jinja2 |
| JenkinsClient／ComposeClient／SecretStore | HTTP、Docker CLI、秘密檔案 |
| DeploymentManager | 組合元件完成啟動／reload／seed |

```bash
uv run --locked python -m unittest discover -s tests -v
uv run --locked python tests/integration.py --pipelines ../jenkins-pipelines
```

整合測試使用獨立 Git fixture、容器與 volumes，結束只清除自己的資源。
預設映像為 pdd-jenkins-controller:req009／pdd-jenkins-agent:req002，可透過命令列覆寫。
