# Deployment Datadog production Sucofindo

Paket tahapan maintenance untuk Ubuntu + Docker, Apache/PHP 8.1, CodeIgniter,
dan MySQL. Jalankan dari host melalui SSH bersama tim Sucofindo. Tidak membutuhkan
Compose utama di workspace. Script tidak mengedit Compose utama, tidak melakukan
recreate aplikasi/database, dan tidak menjalankan `docker-compose down`.

## Metode yang digunakan dan hasil verifikasi dokumentasi

Ditinjau 22 September 2026. Dokumentasi resmi [SSI Docker][ssi] masih menggunakan
installer **di host**, `DD_APM_INSTRUMENTATION_ENABLED=docker`, dan
`DD_NO_AGENT_INSTALL=true` dengan Agent dalam container. Paket mempertahankan
`DD_APM_INSTRUMENTATION_LIBRARIES=php:1` dari development. Tidak ada installer
tracer manual, Dockerfile tracer, atau startup wrapper PHP. File lama yang
memasang tracer secara manual sudah digantikan.

SSI harus menghasilkan default runtime Docker `dd-shim`. Instrumentation baru
diterapkan saat **container dibuat ulang**, bukan sekadar restart. `php:1`
memilih major SDK, bukan pin patch immutable; installer dapat mengambil minor
terbaru. Inventory mencatat package version/path untuk audit.

[Matriks SSI][compat] saat ini mencantumkan Ubuntu 20/22/24 LTS, amd64/arm64,
dan PHP SDK >=1.6.0. Xdebug, ionCube, NewRelic, Blackfire, pcov dan JIT dapat
menghalangi injection. Preflight memeriksa PHP CLI; tim wajib memeriksa SAPI web
karena konfigurasinya dapat berbeda. Paket tidak memaksa `DD_INJECT_FORCE`.
[PHP 8.1 tercantum didukung][php]; paket meminta Agent exact 7.X.Y >=7.76.1
mengikuti rekomendasi keamanan trace metadata pada halaman kompatibilitas tersebut.
Ini perbedaan dari tag development `:7` yang bergerak.

Dokumentasi [korelasi APM–DBM][correlation] mencantumkan PDO untuk PHP. Tracing
MySQLi didukung APM, tetapi keberadaan MySQLi/variabel propagation tidak cukup
membuktikan korelasi. Tim harus memeriksa driver CodeIgniter yang benar-benar
digunakan dan SQL span dari request aplikasi. Paket tidak mengubah driver.

## Isi paket dan prasyarat

- `scripts/datadog-bootstrap`: entry point Bash ke Python standard library >=3.8.
- `config/production.example.json`: konfigurasi non-secret, mapping container dan fitur.
- `config/secrets.example.json`: API key baru, password DBM, password admin opsional.
- `datadog/templates/`: Agent, environment aplikasi, procedure DBM.
- `datadog/mysql/99-datadog.cnf`: startup settings untuk MySQL yang lolos deteksi versi.
- `docs/VALIDATION.md` dan `docs/ROLLBACK.md`: checklist dan rollback per layer.
- `tests/test_deploy.py`: tests offline dengan mock; tidak mengakses Docker/server.

Host: Python 3, Bash, Docker CLI/daemon rootful lokal, curl, tar, sha256sum,
dpkg-query, systemctl. Gunakan root saat memasang SSI; Docker socket memerlukan
akses administrator. Untuk pekerjaan deployment tim, gunakan **`docker-compose`**.
Automation Agent memakai Docker CLI langsung agar tidak bergantung pada Compose tim.
Paket menolak daemon Docker remote/rootless untuk menghindari host SSI berbeda
dari host container. Verifikasi distro/arsitektur container juga pada review compatibility.

Web container: PHP CLI 8.1, Apache (`apache2ctl`, `apachectl`, atau `httpd`),
curl, tar, gzip, gpg dan sh. Database container perlu `mysqld` serta client `mysql`.
Preflight tidak memasang paket yang kurang. Tim memasukkannya ke image mereka.

## 1. Persiapan lokal di host

```bash
cp config/production.example.json config/production.json
cp config/secrets.example.json config/secrets.json
chmod 600 config/secrets.json
bash scripts/datadog-bootstrap discover
```

Isi konfigurasi menggunakan hasil discovery. Nama container dan network harus
eksplisit; paket tidak memilih berdasarkan dugaan. Discovery menampilkan label
`com.docker.compose.service` dan project. Bila label tidak ada, isi parameter
`*_compose_service`; nama container tidak diperlakukan sebagai service name.
Container dengan project berbeda dihentikan saat render agar tim menyusun snippet
per project. Nilai default path adalah contoh yang wajib dicocokkan dengan host.

`discover`/`preflight` hanya membaca host/container dan melakukan koneksi probe.
Tidak menulis laporan secara otomatis; `render` menyimpan discovery terpilih pada
output directory. Tidak ada dump environment container. Hindari membagikan laporan
internal tanpa review walaupun tidak berisi environment/credential.

Isi API key production baru secara lokal; key dari percakapan tidak digunakan.
`admin_password` boleh kosong jika memilih jalur DBA. JSON tidak dieksekusi sebagai
shell dan tidak melakukan ekspansi `$`, backslash, atau command substitution.
Untuk password ber-backslash gunakan escaping JSON biasa (`\\`). Secret tidak
dicetak. Operator yang memiliki akses Docker/root tetap dapat membaca runtime secret.

```bash
bash scripts/datadog-bootstrap preflight
bash scripts/datadog-bootstrap fetch-installers --dry-run
bash scripts/datadog-bootstrap fetch-installers
```

Review file `ssi.sh` dan `rum.sh` di `artifact_dir`, lalu isi `ssi_sha256` dan
`rum_sha256` sesuai hash yang sudah direview. Download tidak mengeksekusi script.
Rerun mempertahankan artifact existing. Hash diverifikasi lagi sebelum eksekusi.
Installer Datadog mengunduh komponen lain; hash entry script **bukan pin seluruh
dependency transitif**. Simpan versi/hashes downstream atau mirror yang disetujui
sesuai change control tim. Jangan menganggap deployment ini reproducible hanya
berdasarkan hash shell installer.

URL [installer RUM resmi][ruminstaller] menggunakan configurator terpisah. Entry
script yang diperiksa mendukung `--agentUri` dan meneruskan argumen lain ke
configurator. Paket menjalankan `--help` configurator pada tahap install dan
memastikan `proxyKind`, `appId`, `site`, `clientToken`, `remoteConfigurationId`,
dan `agentUri` tersedia sebelum install. Jika versi berubah, tahap berhenti.
Review command dari UI RUM production sebagai sumber parameter final.

## 2. Render dan review change

```bash
bash scripts/datadog-bootstrap render --dry-run
bash scripts/datadog-bootstrap render
```

Preflight menentukan versi MySQL sebelum render. Paket menerima Oracle MySQL
5.7, 8.0, 8.4; distro/versi lain dihentikan untuk review, tanpa menebak kesetaraan.
Settings yang disarankan [DBM self-hosted][dbm] sama pada tiga cabang tersebut.
Paket tidak men-upgrade MySQL. Periksa limit memory Performance Schema dan backup
database sebelum maintenance. Mount persistence harus menutupi `mysql_data_dir`;
DBA mengonfirmasi `@@datadir`, termasuk bila path aktual custom.

Output (0600, directory 0700) di `output_dir`:

- `application.override.json`: JSON valid sebagai YAML/Compose. Berisi service labels,
  env, socket mount dan mount MySQL; tidak berisi password/API key.
- `99-datadog.cnf`: Performance Schema ON, tiga batas digest/text 4096,
  current statements/waits dan history consumers.
- `mysql.d/conf.yaml`: JSON valid sebagai YAML Agent, DBM enabled, password DBM.
- `agent-spec.json`: image, network, env non-secret, mount dan capabilities untuk review.
- `discovery.json`: snapshot discovery terpilih, termasuk image/container ID.

Rerender dengan spec/password Agent berubah ditolak; gunakan output directory versi
baru dan migrasi yang direview. File mount yang sama tidak diganti inode saat
isinya identik. Simpan output di path persisten, bukan `/tmp`.

**Handoff pertama:** tim Sucofindo meninjau merge snippet terhadap Compose mereka.
Contoh validasi yang dijalankan tim pada direktori Compose asli:

```bash
docker-compose -f <compose-tim.yml> -f <output-dir>/application.override.json config -q
```

Sesuaikan top-level `version` bila binary `docker-compose` legacy memerlukannya.
Automation tidak menjalankan Compose, membuat network, atau mengubah network
aplikasi. Network existing pilihan harus menghubungkan web/API/MySQL serta Agent.
Resolve `mysql_host` dari alias network database, bukan localhost.

## 3. Agent dan SSI di maintenance window

```bash
bash scripts/datadog-bootstrap agent-start --dry-run
bash scripts/datadog-bootstrap agent-start
sudo bash scripts/datadog-bootstrap ssi-install --dry-run
sudo bash scripts/datadog-bootstrap ssi-install --maintenance
```

Jalankan sudo dari direktori paket atau berikan path `--config`/`--secrets` absolut.
Agent dibuat dengan restart `unless-stopped`, network existing, socket APM/DogStatsD,
host/container metrics, dan log collection. Tidak ada port host publik yang dibuka.
PHP mengirim trace melalui `unix:///var/run/datadog/apm.socket`; RUM mengakses HTTP
`http://<agent_name>:8126` dalam network Docker. Socket host dibind ke aplikasi;
verifikasi permission sebagai user worker Apache/PHP-FPM, bukan hanya root/CLI.

Agent existing tidak dihapus/diganti. Agent milik paket dengan fingerprint sama
hanya dicek health; spec berbeda atau Agent lain memerlukan review tim. Rotasi API
key adalah perubahan spec dan tidak dilakukan diam-diam. Deteksi berbasis nama/image
Agent dan service host; custom image tanpa penanda perlu audit operator pada inventory.

SSI existing dengan `dd-shim` dan paket PHP dideteksi serta dilewati. Instalasi
parsial dihentikan. Backup daemon.json/preload tersedia sebelum install; installer
host sendiri dapat mengubah konfigurasi/runtime Docker, sehingga tahap wajib berada
dalam maintenance window. Pada timeout, periksa proses installer dan state Docker
sebelum mengulang; timeout CLI bukan rollback transaksi.

**Handoff kedua:** setelah SSI siap, tim memasukkan env/socket mount dan melakukan
recreate web/API. Jika startup config MySQL baru ditambahkan sebagai mount, tim
recreate MySQL dengan volume data yang sama; jika mount sudah ada dan hanya isi
konfigurasinya berubah, restart MySQL cukup. Environment/image/mount baru membutuhkan
recreate. Paket tidak menjalankan tindakan lifecycle tersebut.

## 4. SQL DBM (pilih salah satu jalur)

Jalur operator dengan credential admin lokal:

```bash
bash scripts/datadog-bootstrap dbm-apply --dry-run
bash scripts/datadog-bootstrap dbm-apply --maintenance
```

Checks dilakukan sebelum SQL mutasi: versi server/datadir/schema aplikasi, account
existing dan login monitoring, limit koneksi existing, definisi/signature/security
procedure. Password existing tidak diubah. Procedure berbeda/tidak terbaca tidak
ditimpa. Account baru dibatasi lima koneksi. Grant dasar PROCESS, REPLICATION CLIENT,
SELECT performance_schema, SELECT index stats, serta EXECUTE procedure. Tidak memberi
SELECT data aplikasi. Schema metadata collection/REFERENCES tidak diaktifkan default;
ini fitur DBM opsional dan bisa ditambahkan dalam change terpisah.

SQL literal memakai mode session `NO_BACKSLASH_ESCAPES` eksplisit dan quote doubling;
identifier divalidasi ketat. Password diberikan lewat stdin client, bukan argv.
Credential masih berada sesaat dalam environment proses client MySQL di container,
yang dapat dilihat root. Output error SQL ditahan agar tidak membocorkan password.
Procedure memakai `SQL SECURITY DEFINER`: DBA meninjau account definer beserta haknya.
User admin/definer jangan dihapus setelah provisioning. DDL tidak transaksional;
kegagalan sebagian dapat dilanjutkan setelah pemeriksaan rerun.

Jalur DBA tanpa memberi operator credential admin:

```bash
bash scripts/datadog-bootstrap dbm-export
```

Serahkan `dbm-dba-review.sql` melalui kanal aman. DBA membaca SELECT awal untuk
versi/datadir, memeriksa schema, user/password/grant existing dan
`SHOW CREATE PROCEDURE`. Hapus hanya blok CREATE procedure yang sudah identik;
jika berbeda hentikan untuk review. Jalankan client **tanpa `--force`**. File ini
template review, bukan migrasi blind rerun; tidak berisi DROP/rotasi password.
Sesudah DBA selesai, lanjut `verify` memakai monitoring password. Startup settings
dan SQL adalah tahap terpisah; SQL provisioning tidak me-restart database.

## 5. RUM dan persistence

Isi application ID, public client token, dan remote configuration ID khusus production.
[Apache auto-injection][rum] masih preview; periksa site/account dan egress sebelum
maintenance. Agent harus sudah reachable dari web.

```bash
bash scripts/datadog-bootstrap rum-install --dry-run
bash scripts/datadog-bootstrap rum-install --maintenance
```

Script mem-backup Apache root, memeriksa flag configurator, menjalankan installer,
menambahkan `DatadogTracing Off` agar module Apache fokus RUM sementara PHP memakai
SSI, melakukan config test, lalu graceful reload. Backup memiliki manifest container.
Jika installer gagal atau config test gagal, exit nonzero; tidak ada rollback otomatis.
Review perilaku reload installer downstream sebelum menjalankannya—wrapper hanya
mengontrol reload miliknya sendiri. Existing module dilewati agar tidak diinstal ganda;
tim harus memeriksa bahwa config existing benar-benar milik RUM production.

Output `rum-persistence-*` mengekspor Apache config dan `/opt/datadog-httpd`, disertai
contoh Dockerfile **khusus RUM** dan snippet bind mount sebagai alternatif. Review
LoadModule/Include, seluruh external assets/library dependencies, symlink, ownership,
base image dan ABI Apache; jangan langsung menyalin seluruh config production ke image
yang dipublikasikan. Ekspor dapat memuat virtual host/TLS config, sehingga diperlakukan
sensitif. Pilihan yang disarankan: cherry-pick delta RUM ke image web tim berdasarkan
base image yang sama, restore USER asli, lalu config test/build di staging. Alternatif
mount menyimpan seluruh Apache config di host dan membutuhkan pengelolaan config
tersebut pada deploy berikutnya. Jangan memakai kedua metode sekaligus.

**Handoff ketiga:** tim membangun/deploy image RUM persisten atau memasang mount yang
sudah direview, recreate web, lalu menjalankan verifikasi kembali. `docker exec`
saat install awal tidak dianggap persistence. Jika tahap export gagal, installer
mungkin sudah terpasang; gunakan `rum-export` untuk melanjutkan tanpa reinstall.

## 6. Validasi dan hasil yang boleh dinyatakan

```bash
bash scripts/datadog-bootstrap verify
bash scripts/datadog-bootstrap smoke --dry-run
bash scripts/datadog-bootstrap smoke
```

`verify` memeriksa Agent health, runtime SSI, tracer CLI, socket access, env allowlist,
Performance Schema, consumer, executable explain procedure, MySQL check JSON, modul
Apache dan endpoint Agent. Error penting menghasilkan exit 1. `smoke` mengirim GET
ke endpoint yang dipilih operator: wajib endpoint baca yang benar-benar menjalankan
SQL; response tidak dicetak. Marker SDK dalam HTML membuktikan injection saja.

`verify` hanya memberi PASS untuk checks lokal yang dijalankan. Section status
opsional perlu review sesuai [checklist](docs/VALIDATION.md); CLI extension tidak
membuktikan SAPI HTTP atau telemetry terkirim. Browser harus menghasilkan session,
request API dan SQL; verifikasi tautan RUM–APM dan APM–DBM di Datadog. Konfigurasi
Allowed Tracing URLs, sampling, propagator, CSP, serta CORS dilakukan sesuai aplikasi,
bukan disimpulkan dari module RUM yang sudah loaded.

Semua stage mendukung `--dry-run`. Mode ini tidak menulis file, mengunduh installer,
menjalankan SQL mutasi, memasang modul, reload/recreate, atau memulai container.
Ia masih membutuhkan host/config yang valid dan melakukan discovery/probe read-only;
`dbm-apply --dry-run` memerlukan credential admin untuk read checks. Tidak ada
deployment remote atau SSH otomatis.

## Fitur Agent vs baseline development

| Baseline | Implementasi production |
| --- | --- |
| Infrastructure/APM, DogStatsD non-local, sockets | Aktif; network/socket parameter dan tidak publish host ports |
| Container logs all + auto multiline | Aktif; Agent sendiri dikecualikan berdasarkan nama parameter |
| Process collection | `process_collection=true`; membutuhkan host PID dan passwd/group read-only |
| Runtime security | `runtime_security=false` default; opt-in terpisah |
| Network monitoring | `network_monitoring=false` default; opt-in terpisah |
| Universal Service Monitoring | `universal_service_monitoring=false` default; opt-in terpisah |
| Host root/os-release | Dipasang bila runtime security atau USM dipilih |
| debugfs, host cgroup, apparmor unconfined | Hanya jika salah satu fitur system-probe dipilih |
| SYS_ADMIN, SYS_RESOURCE, SYS_PTRACE, NET_ADMIN, NET_BROADCAST, NET_RAW, IPC_LOCK, CHOWN | Dipasang untuk opsi system-probe, sesuai contoh resmi; merupakan akses host luas |
| KILL | Tidak ditambahkan: automated response tidak termasuk scope; tidak diperlukan pada contoh setup monitoring resmi |
| Agent run dir, docker socket/proc/cgroup/container logs | Dipertahankan, host paths diparameterkan |

Tiga fitur opt-in bukan otomatis aktif di production. Aktifkan masing-masing setelah
review kebutuhan, kernel/eBPF, billing, privileges, dan health. Tidak memakai
`--privileged`. [CNM][cnm], [USM][usm] dan [Workload Protection][security] menjelaskan
capabilities dan kebutuhan platform. Jika kernel memerlukan header/runtime compilation,
tim menambahkan mount kernel headers sesuai dokumentasi; paket tidak memasang paket
kernel atau melemahkan host policy otomatis.

## Uji lokal dan rollback

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Tests mencakup validation, encoding secret, label/service mapping, dry-run, render
rerun, SQL existing/conflict, checksum, guard Agent/SSI, timeout dan failure redaction.
Tidak membutuhkan Docker/credential sungguhan. Tests mock tidak menggantikan integration
test pada Ubuntu/Apache/MySQL dan traffic production. Jalankan acceptance di staging
yang setara sebelum maintenance production. Panduan rollback per layer ada pada
[docs/ROLLBACK.md](docs/ROLLBACK.md); database data/volume tidak pernah dihapus paket.

## Referensi resmi

- [SSI Docker][ssi] dan [compatibility SSI][compat].
- [PHP compatibility][php] dan [korelasi APM–DBM][correlation].
- [Self-hosted MySQL DBM][dbm].
- [Apache RUM][rum], [installer RUM][ruminstaller], [RUM–APM][rumapm].
- [CNM][cnm], [USM][usm], [Workload Protection Docker][security].
- [MySQL string literals][mysqlstrings], [CREATE PROCEDURE][mysqlproc],
  [account/grants][mysqlusers], [Performance Schema startup][mysqlps].

[ssi]: https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/docker/
[compat]: https://docs.datadoghq.com/tracing/trace_collection/single-step-apm/compatibility/
[php]: https://docs.datadoghq.com/tracing/trace_collection/compatibility/php/
[correlation]: https://docs.datadoghq.com/database_monitoring/connect_dbm_and_apm/
[dbm]: https://docs.datadoghq.com/database_monitoring/setup_mysql/selfhosted/
[rum]: https://docs.datadoghq.com/real_user_monitoring/application_monitoring/browser/setup/server/apache/
[ruminstaller]: https://rum-auto-instrumentation.s3.amazonaws.com/installer/latest/install-proxy-datadog.sh
[rumapm]: https://docs.datadoghq.com/tracing/other_telemetry/rum/
[cnm]: https://docs.datadoghq.com/network_monitoring/cloud_network_monitoring/setup/
[usm]: https://docs.datadoghq.com/universal_service_monitoring/setup/
[security]: https://docs.datadoghq.com/security/workload_protection/setup/docker/
[mysqlstrings]: https://dev.mysql.com/doc/refman/8.0/en/string-literals.html
[mysqlproc]: https://dev.mysql.com/doc/refman/8.0/en/create-procedure.html
[mysqlusers]: https://dev.mysql.com/doc/mysql-security-excerpt/8.0/en/creating-accounts.html
[mysqlps]: https://dev.mysql.com/doc/refman/8.0/en/performance-schema-quick-start.html
