# Развёртывание мини-сервиса поддержки на VPS (День 33)

Сервис — один контейнер `ai-advent-support:33`: чат пользователя `/support`, пульт сотрудника
`/support/desk`, API, ассистент на ds-flash, поиск BM25 по FAQ и документации портала. Локальной
модели и модели эмбеддингов в образе нет — поэтому хватает самого маленького VPS.

## Минимальные требования к VPS

| | Минимум | Замечание |
|---|---|---|
| Процессор | 1 vCPU | ответ ≈ 5 с — почти всё время ждёт модель по сети |
| Память | 1 ГБ + swap 1 ГБ | контейнер — ≈ 95 МБ под запросом, лимит 400 МБ |
| Диск | 5 ГБ свободно | образ 244 МБ, Docker ≈ 1 ГБ, данные и журналы — мегабайты |
| ОС | Ubuntu 22.04+ / Debian 12 | Docker 24+ с `docker compose` |
| Сеть | входящий TCP 8450 (или 443); исходящий HTTPS к `api.deepseek.com` | домен — по желанию |

Образ **собирается дома**: в нём база знаний `index-support/chunks.json` — фрагменты
документации портала, которые в git не хранятся. На VPS из git ничего не собирается.

## 1. Дома: база знаний и образ

```bash
cd ~/Документы/AI-Advent-Challenge/Day-33
.venv/bin/python cli.py --поддержка-индекс           # FAQ + документация CRM → index-support/chunks.json
docker build -t ai-advent-support:33 .
```

## 2. Доступ к VPS по ключу (один раз)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/day33_vps -N "" -C day33-vps
ssh-copy-id -i ~/.ssh/day33_vps.pub -p 15259 root@93.88.203.91      # спросит пароль один раз
cat >> ~/.ssh/config <<'EOF'
Host day33-vps
  HostName 93.88.203.91
  Port 15259
  User root
  IdentityFile ~/.ssh/day33_vps
  IdentitiesOnly yes
EOF
```

После этого пароль root можно сменить, а вход по паролю — выключить (`PasswordAuthentication no`
в `/etc/ssh/sshd_config`, затем `systemctl reload ssh`) — доступ останется по ключу.

## 3. VPS: Docker, swap, порт

```bash
ssh day33-vps '
  command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
  swapon --show | grep -q . || { fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo "/swapfile none swap sw 0 0" >> /etc/fstab; }
  command -v ufw >/dev/null && ufw allow 8450/tcp || true
  mkdir -p /opt/support'
```

## 4. Перенос образа и запуск

```bash
docker save ai-advent-support:33 | gzip | ssh day33-vps 'gunzip | docker load'
scp -P 15259 docker-compose.yml day33-vps:/opt/support/          # или scp с Host day33-vps
grep '^DEEPSEEK_API_KEY=' .env | ssh day33-vps 'umask 077; cat > /opt/support/.env.support'
ssh day33-vps 'cd /opt/support && SUPPORT_TLS_HOSTS=93.88.203.91 docker compose up -d && docker compose ps'
```

Сертификат сервис выпускает сам своим центром сертификации (в томе `/data/tls`, адреса —
127.0.0.1 и `SUPPORT_TLS_HOSTS`). Браузер предупредит о неизвестном центре; чтобы не
предупреждал — установите `ca.crt` из тома (`docker compose cp support:/data/tls/ca.crt .`) или
поставьте перед сервисом обратный прокси с доменом и Let's Encrypt (Caddy) и запускайте сервис
с `--http` за ним.

## 5. Ключи доступа

```bash
ssh day33-vps 'cd /opt/support && docker compose exec support python support_app.py --ключ сотрудник поддержка-1'
ssh day33-vps 'cd /opt/support && docker compose exec support python support_app.py --ключ пользователь u01'
```

Каждая команда печатает ключ один раз и путь `/support…?key=…` — откройте
`https://93.88.203.91:8450` + этот путь. Ключ в адресе нужен только на первый вход: сервис ставит
cookie и убирает ключ из адреса. Данные — синтетические (`support-seed.json`); сбросить —
`docker compose exec support python support_app.py --сброс`.

## 6. Проверка

```bash
.venv/bin/python check_support.py --адрес https://93.88.203.91:8450 --части 10
```

Часть 10 проверяет здоровье, вход по ключу (ключ в адресе не остаётся), ответ ассистента по тикету,
границы (чужой тикет — 404), заявку и пульт, память контейнера.

## Обновление

Собрать образ дома заново (шаг 1), перенести и `docker compose up -d` (шаг 4). Том `support-data`
с тикетами, ключами и журналами сохраняется.
