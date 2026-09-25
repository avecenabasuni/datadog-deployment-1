# Lab untuk mencoba command deployment production

Lab ini memakai coordinator yang sama dengan `eminerba-production`, tetapi dengan
`env:lab`, Agent/service khusus lab, aplikasi dummy, dan database contoh.
Tidak memerlukan source, backup database, atau kredensial production.

Gunakan VM Ubuntu 22.04/24.04 khusus lab, Docker rootful, Compose, Python 3,
Bash, curl, dan akses sudo. Alokasi awal: 4 vCPU, RAM 8 GB, disk kosong 25 GB.
`docker compose` dan `docker-compose` terdeteksi otomatis. Jika memakai legacy
`docker-compose` 1.29, tambahkan paket `python3-yaml` untuk validasi model Compose.
Ambil snapshot sebelum SSI. PHP 8.1 digunakan untuk mencocokkan target, bukan
sebagai rekomendasi versi aplikasi baru.

**Lab lama dengan container `eminerba-lab-*` bukan fixture ini.** Setup akan menolak
container lain, termasuk lab lama atau Agent host, tanpa menghapusnya. Gunakan
VM bersih atau snapshot bersih; jangan jalankan dua Agent dalam satu VM.

## 1. Buat dan jalankan aplikasi dummy

Dari folder repository di VM:

```bash
git pull origin main
sudo bash scripts/eminerba-lab prepare
```

Command ini membuat fixture, build web/API, menjalankan MySQL, menunggu query
baseline berhasil, membuat konfigurasi observability lab, dan mengunduh installer
untuk review. Build pertama dapat memerlukan beberapa menit.

Struktur yang dibuat:

```text
/opt/eminerba-rehearsal/
├── stack/
│   ├── docker-compose.yml
│   ├── .env
│   ├── .eminerba-lab.json
│   ├── docker/
│   │   ├── DockerFile
│   │   ├── apache.conf
│   │   ├── php.ini
│   │   ├── my.cnf
│   │   └── 001-seed.sql
│   ├── pit_to_port/
│   └── apiminerba/
├── artifacts/
└── generated/                 # dibuat saat deployment
```

Service tetap `web`, `api`, dan `db`, dengan nama container production
`eminerba_web`, `eminerba_api`, dan `eminerba_db`. Project-nya berbeda:
`eminerba-rehearsal`. Volume datanya `eminerba-rehearsal_mysql_data`.
`vendor_data` dan `php_sessions` juga dideklarasikan mengikuti layout production.

Web hanya dipublikasikan di `127.0.0.1:8081`, API di `127.0.0.1:8080`, dan MySQL
tidak dipublikasikan. Tidak ada perubahan ke `/opt/eminerba_docker`.

Fixture menggunakan image awal `php:8.1-apache-bookworm` dan `mysql:8.0`.
Tag ini bukan immutable pin. Saat instrumentation diterapkan, coordinator mem-pin
image ID yang sudah berjalan. Image build/pull tetap harus berhasil pada VM.
Compose ditulis sebagai JSON yang valid untuk format YAML Compose.

Password database dummy dibuat secara acak dan disimpan privat. Aplikasi hanya
memiliki akses SELECT ke data contoh. Jangan mengedit `.env` atau Compose hasil
generator: marker menolak perubahan agar fixture tidak berubah menjadi target
lain tanpa sengaja. `prepare` ulang mempertahankan file/password dan tidak
membuat ulang baseline setelah konfigurasi observability tersedia.

## 2. Isi konfigurasi Datadog sekali

```bash
sudo nano config/eminerba-rehearsal.json
sudo nano config/eminerba-rehearsal-secrets.json
```

Yang perlu kamu isi:

| File | Field |
| --- | --- |
| `eminerba-rehearsal.json` | `agent_image` versi eksak 7.X.Y >=7.76.1, `site` |
| `eminerba-rehearsal.json` | `rum_application_id`, `rum_client_token`, `rum_remote_configuration_id` khusus lab |
| `eminerba-rehearsal.json` | `ssi_sha256`, `rum_sha256` setelah review installer |
| `eminerba-rehearsal-secrets.json` | `api_key` Datadog lab |

Environment, service, URLs, schema, versi dummy, password admin MySQL, dan password
DBM sudah diisi otomatis. Pertahankan password hasil generator. File konfigurasi
ini diabaikan Git; jangan menempel isinya yang mengandung secret ke chat/log.

Review installer dan download lanjutan yang dipanggilnya:

```bash
sudo less /opt/eminerba-rehearsal/artifacts/ssi.sh
sudo less /opt/eminerba-rehearsal/artifacts/rum.sh
sudo sha256sum /opt/eminerba-rehearsal/artifacts/ssi.sh \
  /opt/eminerba-rehearsal/artifacts/rum.sh
```

Hash entry script tidak mem-pin semua dependency yang diunduh. Datadog API key
terpisah tidak otomatis membuat organisasi atau kuota billing yang terpisah.

## 3. Preview dan deployment satu command

```bash
sudo bash scripts/eminerba-lab --dry-run
```

Jika pemeriksaan lolos:

```bash
sudo bash scripts/eminerba-lab --maintenance
```

Command menjalankan alur production yang sama: pemeriksaan resource existing,
render, dependensi image, Agent, SSI, recreation DB, readiness/startup checks,
DBM, recreation API/web, RUM, persistence RUM, lalu verify dan smoke.
Tidak perlu mendefinisikan helper `dc` atau `dd_lab` di sesi shell.

Agent lab bernama `eminerba-rehearsal-agent`. Semua telemetry memakai `env:lab`
dengan service `eminerba-rehearsal-web` dan `eminerba-rehearsal-api`.
Ulangi command hanya sebagai tindakan maintenance: container dapat direcreate
lagi. Kegagalan menghentikan urutan; tidak ada rollback atau penghapusan data otomatis.

## 4. Uji dari browser

Di laptop, buat SSH tunnel dan biarkan berjalan:

```bash
ssh -N -L 8081:127.0.0.1:8081 USER@IP_VM_LAB
```

Buka `http://localhost:8081/`, lalu klik **Run read-only SQL request**.
Set Allowed Tracing URLs RUM lab untuk `http://localhost:8081/api/`.
Browser mengakses API melalui Apache proxy dengan origin yang sama.

Diagnostik lokal VM:

```bash
curl --fail http://127.0.0.1:8081/health.php
curl --fail http://127.0.0.1:8081/api/health.php
sudo docker exec eminerba-rehearsal-agent agent status
```

Cari `env:lab` di Datadog dan buktikan host/container, logs, HTTP traces dengan SQL
spans, DBM query samples, serta sesi RUM dan korelasinya. `verify` dan `smoke`
hanya membuktikan pemeriksaan lokal, bukan semua telemetry/korelasi.

## Batas pengujian dan menghentikan lab

Fixture ini meniru topologi dan mount production. Aplikasi dummy menggunakan PDO;
tidak membuktikan perilaku CodeIgniter, MySQLi, autentikasi, proxy production, atau
CORS lintas origin. Data hanya dua schema contoh, bukan salinan data production.
Tidak ada tracer atau RUM SDK manual yang ditanam di aplikasi dummy.

Untuk stop tanpa menghapus data, dari repo:

```bash
sudo bash scripts/compose \
  --project-directory /opt/eminerba-rehearsal/stack \
  --env-file /opt/eminerba-rehearsal/stack/.env \
  -p eminerba-rehearsal \
  -f /opt/eminerba-rehearsal/stack/docker-compose.yml stop
sudo docker stop eminerba-rehearsal-agent
```

SSI masih terpasang pada host setelah stop. Untuk kembali bersih, gunakan snapshot
awal atau [rollback SSI](../docs/ROLLBACK.md). Hindari prune dan `down -v` jika
data ingin dipertahankan. Saat membuat ulang/menjalankan stack setelah instrumentasi,
sertakan `/opt/eminerba-rehearsal/generated/production.override.json`; base Compose
saja dapat menghilangkan konfigurasi instrumentasi. `prepare` bukan command resume
untuk container yang sudah distop.
