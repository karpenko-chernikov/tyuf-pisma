# Деплой на сервер rosypt.ru

Сайт писем: **https://pisma.rosypt.ru** (поддомен к тому же nginx, что и rosypt.ru).

## На сервере (один раз)

```bash
# код
sudo mkdir -p /var/www/tyuf-pisma
sudo chown "$USER":"$USER" /var/www/tyuf-pisma
cd /var/www/tyuf-pisma
git clone https://github.com/karpenko-chernikov/tyuf-pisma.git .
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# секреты — не в git
cp .env.example .env
nano .env   # SECRET_KEY и пароли NIKITA_ / ZINKEVICH_ / GOLOMOLZIN_

# systemd
sudo cp deploy/tyuf-pisma.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tyuf-pisma

# nginx + TLS
sudo cp deploy/nginx-pisma.rosypt.ru.conf /etc/nginx/sites-available/pisma.rosypt.ru
sudo ln -sf /etc/nginx/sites-available/pisma.rosypt.ru /etc/nginx/sites-enabled/
# DNS: A-запись pisma.rosypt.ru → IP сервера
sudo certbot --nginx -d pisma.rosypt.ru
sudo nginx -t && sudo systemctl reload nginx
```

## Обновление

```bash
cd /var/www/tyuf-pisma
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart tyuf-pisma
```

База `data/pisma.db` и `.env` git не трогает.
