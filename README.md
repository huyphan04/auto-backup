# Scorex Backup — PostgreSQL → SharePoint (2 giờ/lần)

## Thành phần

| File | Chức năng |
|---|---|
| `scripts/backup_postgres.sh` | `pg_dump` + gzip database, lưu vào `dumps/`, dọn file cục bộ cũ hơn 24h |
| `scripts/upload_sharepoint.py` | Lấy token Azure AD (client credentials), upload file lên SharePoint qua Microsoft Graph (tự chuyển sang upload session nếu file > 4MB) |
| `scripts/run_backup.sh` | Gọi lần lượt 2 script trên, ghi log |
| `.env.example` | Cấu hình DB + Azure AD + đường dẫn SharePoint (đã điền sẵn theo thông tin bạn cung cấp, nhắm tới `HDBANK/PRD`) |
| `systemd/scorex-backup.service` | Unit chạy `run_backup.sh` với user `backup_user` |
| `systemd/scorex-backup.timer` | Kích hoạt service mỗi 2 giờ (`OnCalendar=*-*-* 00/2:00:00`) |
| `install.sh` | Cài đặt toàn bộ: tạo user, thư mục, **file log với quyền đúng trước khi chạy lần đầu**, copy file, bật timer |

## Đặt tên riêng cho từng dự án

Service/timer, thư mục cài đặt, và log file đều được **sinh tự động từ biến `PROJECT_NAME`** trong `.env`, không phải sửa tay từng file:

| Biến | Sinh ra |
|---|---|
| `PROJECT_NAME=scorex` | `scorex-backup.service`, `scorex-backup.timer`, `/opt/scorex-backup/`, `/var/log/scorex-backup.log` |
| `PROJECT_NAME=hdbank-crm` | `hdbank-crm-backup.service`, `hdbank-crm-backup.timer`, `/opt/hdbank-crm-backup/`, `/var/log/hdbank-crm-backup.log` |

Cách dùng cho dự án thứ 2 trở đi (chạy song song, không đụng nhau):

```bash
# copy toàn bộ thư mục template sang bản mới cho dự án khác
cp -r scorex-backup another-project-backup
cd another-project-backup

# sửa .env: đổi PROJECT_NAME, POSTGRES_DATABASE, SHAREPOINT_FOLDER cho đúng dự án
nano .env.example

sudo ./install.sh
```

Hoặc chỉ định tên ngay khi cài, không cần sửa `.env` trước:
```bash
sudo ./install.sh hdbank-crm
```
(tham số dòng lệnh sẽ ưu tiên hơn giá trị `PROJECT_NAME` trong `.env`)

`BACKUP_USER` (mặc định `backup_user`) có thể dùng chung cho nhiều dự án, hoặc đặt riêng mỗi dự án một user nếu muốn cô lập quyền. `BACKUP_INTERVAL_HOURS` (mặc định `2`) cũng chỉnh được theo từng dự án.

## Cài đặt trên VPS (local_scorex)

```bash
# copy thư mục này lên VPS, ví dụ qua scp
scp -P 2522 -r scorex-backup root@58.186.3.212:/root/

ssh root@58.186.3.212 -p 2522
cd /root/scorex-backup
chmod +x install.sh scripts/*.sh
sudo ./install.sh
```

Script `install.sh` sẽ:
1. Tạo user hệ thống `backup_user` (không có quyền login shell)
2. Tạo `/opt/scorex-backup/{scripts,dumps}`
3. **Tạo `/var/log/scorex-backup.log` và set quyền cho `backup_user` trước khi service chạy lần đầu** — đây là bước quan trọng để tránh lỗi `PermissionError` đã gặp trước đó
4. Copy `.env.example` → `/opt/scorex-backup/.env` (nếu chưa có `.env` sẵn)
5. Cài `requests` cho Python
6. Cài và bật `scorex-backup.timer`

## Kiểm tra sau khi cài

```bash
systemctl status scorex-backup.timer
systemctl list-timers scorex-backup.timer      # xem lần chạy kế tiếp
sudo -u backup_user /opt/scorex-backup/scripts/run_backup.sh   # chạy thử ngay
tail -f /var/log/scorex-backup.log
```

## Đổi sang môi trường DEV

Nếu cần một job backup riêng cho DEV, tạo thêm 1 bộ `.env` với:
```
SHAREPOINT_FOLDER=HDBANK/DEV
POSTGRES_DATABASE=<tên db dev>
```
và một cặp service/timer riêng (đổi tên, đổi `ExecStart` trỏ tới thư mục cấu hình DEV).

## Lưu ý bảo mật

Các thông tin nhạy cảm (mật khẩu DB, Client Secret Azure AD, mật khẩu SSH/SharePoint) đã được dán trực tiếp vào cấu hình `.env` để tiện triển khai. Vì các giá trị này đã xuất hiện trong hội thoại, bạn nên cân nhắc xoay vòng (rotate) Client Secret và các mật khẩu liên quan sau khi triển khai xong, và giữ file `.env` ở quyền `600` (script đã tự set).
