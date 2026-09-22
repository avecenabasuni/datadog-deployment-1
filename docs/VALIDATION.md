# Checklist acceptance Sucofindo

Catat waktu window, operator, release aplikasi, image digest Agent, versi SSI/PHP
dan nomor change. Jangan menyimpan environment/password atau isi request pengguna
di checklist ini. Status akhir belum ACCEPTED sampai bukti runtime/telemetry selesai.

## Sebelum mutasi

- [ ] Inventory Ubuntu/kernel/arsitektur/Docker sesuai matrix resmi.
- [ ] Container web, API, DB, Compose labels/project, dan network disetujui tim.
- [ ] Audit seluruh inventory untuk Agent custom image/host Agent; hanya satu Agent per host.
- [ ] Tidak ada tracer PHP manual/extension bentrok di image maupun konfigurasi SAPI web.
- [ ] PHP CLI **dan SAPI web** 8.1; OPcache JIT/Xdebug/ionCube/NewRelic/Blackfire/pcov tidak menghalangi SSI.
- [ ] MySQL server `SELECT VERSION(), @@datadir` cocok dengan discovery; bukan MariaDB/Percona tak direview.
- [ ] Datadir yang sebenarnya ditutupi volume/bind persisten; backup database dan restore procedure tim tersedia.
- [ ] Check include directory MySQL benar-benar membaca target `99-datadog.cnf`; jangan berasumsi semua image membaca `/etc/mysql/conf.d`.
- [ ] Driver **yang dipakai CodeIgniter pada request** ditentukan (PDO MySQL vs MySQLi), bukan hanya extension yang tersedia.
- [ ] API key baru/site production, RUM application/client token/config ID production sudah benar.
- [ ] Kedua entry installer serta download transitif direview; hash dicatat. RUM configurator help mengenali semua flags.
- [ ] Egress HTTPS/TLS ke registry, installer, Datadog intake, RUM CDN/intake diizinkan; tidak menonaktifkan TLS verification.
- [ ] Opsi security/CNM/USM dan privilege host direview terpisah; pemilik menyetujui scope telemetry/billing.
- [ ] Tim review snippet merge dengan `docker-compose ... config -q`; tidak mencetak full config yang mungkin punya secret.

## Setelah handoff recreate

- [ ] Docker default runtime `dd-shim`; web/API container **baru** menggunakan runtime tersebut.
- [ ] PHP tracer versi >=1.6 muncul di SAPI HTTP; CLI bukan pengganti test request.
- [ ] Env DD_ENV=prod, service/version, DBM propagation ada pada worker web/API.
- [ ] PHP-FPM `clear_env` atau Apache env handling tidak membuang env instrumentation.
- [ ] Socket `/var/run/datadog/apm.socket` ada di host/Agent/aplikasi. Worker UID Apache/FPM dapat connect; jangan chmod 777 tanpa review.
- [ ] Agent HTTP `http://<agent_name>:8126/info` reachable dari web; MySQL reachable langsung dari Agent melalui alias network.
- [ ] MySQL Performance Schema ON dan tiga digest/text sizes 4096; current statements/waits + history-long enabled.
- [ ] Account/grants/limit koneksi dan definer procedure diverifikasi DBA. Tidak ada rotasi password tak direncanakan.
- [ ] Procedure explain berjalan pada schema datadog dan schema aplikasi menggunakan user DBM.
- [ ] `verify` lulus; `agent check mysql --json` tidak berisi error; config loaded host/user/dbm cocok dengan target tanpa membagikan password.

## Agent feature health (manual, bukan sekadar section presence)

Jalankan `docker exec <agent> agent status` secara lokal. Jangan mengunggah output
mentah sebelum redaksi. Section name/JSON dapat berbeda antar versi Agent.

- [ ] Collector/forwarder sehat, API key accepted dan tidak ada error intake.
- [ ] APM Agent Running, receiver menerima traces setelah request API/web.
- [ ] Logs Agent Running, input container yang benar active, bytes/logs sent bertambah.
- [ ] Process Agent Running bila dipilih, process/container inventory tampil di Datadog.
- [ ] Bila CNM dipilih: system-probe/network module berhasil start, tidak ada eBPF/permission/kernel error, data koneksi terlihat.
- [ ] Bila USM dipilih: service-monitoring module sehat dan HTTP services teramati; ini tidak menggantikan trace PHP SSI.
- [ ] Bila runtime security dipilih: security-agent/runtime module sehat dan workload terlihat. Self-test/alert test hanya sesuai prosedur tim.
- [ ] Bandingkan konsumsi CPU/memory/disk/log volume dengan baseline sebelum window.

## RUM persistence dan injection

- [ ] Apache config test lulus sebelum graceful reload, modul Datadog termuat.
- [ ] DatadogTracing Off untuk module Apache; APM PHP tetap berasal dari SSI.
- [ ] RUM settings/ID/token/site/remoteConfigurationId production dikonfirmasi pada config terpasang.
- [ ] HTTP response HTML mengandung injeksi; tidak ada duplikasi SDK manual yang sebelumnya ditanam aplikasi.
- [ ] CSP mengizinkan script/connect yang diperlukan. Jika Apache proxy, compression/TLS upstream tidak menghalangi filter body.
- [ ] RUM config/module sudah masuk image tim atau mount persisten yang direview, termasuk external LoadModule/Include/library dependencies.
- [ ] Recreate dari deployment resmi tim lalu ulangi config test + modul + HTTP injection; bukti instalasi persisten disimpan.
- [ ] Backup/export Apache tidak mengikutsertakan credential/TLS key ke registry publik atau source control.

## Telemetry end-to-end (wajib, tidak diautomasi dengan credential API tambahan)

1. Buka browser production dan jalankan alur baca yang memanggil API dan MySQL.
   Catat timestamp, service, resource, status tanpa PII; jangan memakai request tulis untuk smoke.
2. Di Datadog lihat infrastructure host/container, log nyata dari web/API, dan APM
   trace HTTP dengan SQL child span. Trace CLI saja tidak memenuhi acceptance.
3. Untuk RUM–APM, konfigurasi Allowed Tracing URLs hanya origin/path internal yang
   diperlukan di UI RUM, trace/session sampling yang disepakati dan propagator.
   Pastikan request browser mengirim trace headers; proxy tidak membuangnya.
   Untuk lintas origin, CORS OPTIONS dan Access-Control-Allow-Headers mencakup header
   yang benar-benar dipakai: traceparent/tracestate dan/atau x-datadog-trace-id,
   x-datadog-parent-id, x-datadog-origin, x-datadog-sampling-priority (baggage jika dipakai).
   Jangan wildcard seluruh domain. Bukti: resource RUM membuka trace backend yang sama.
4. Untuk APM–DBM, pastikan driver aktual didukung propagation dan query memiliki
   context trace. Cari query sample DBM dan hubungan ke SQL span yang sama; sampling
   dapat membuat satu request tidak muncul. Gunakan traffic baca berulang dengan batas
   waktu yang disepakati, bukan loop tak terbatas. MySQLi-only: status korelasi PENDING
   sampai dukungan versi aktual dan bukti berhasil tersedia; jangan mengubah driver otomatis.
5. Verifikasi explain plan untuk query tabel nyata yang aman, bukan hanya SELECT 1.
6. Amati error rate/latency/resource selama interval tim (contoh 15 menit). Jika ada
   regresi atau instrumentation ganda, ikuti rollback layer terkait.

Catatan status: `verify` exit 0 berarti checks lokal yang disebut script lulus;
tidak sama dengan deployment end-to-end ACCEPTED. Poin manual yang gagal tetap
menghalangi sign-off walaupun script exit 0.
