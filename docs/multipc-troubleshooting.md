# Multi-PC Troubleshooting: Docker sin acceso a la LAN

## Síntoma

Desde el host funciona, pero desde dentro de un contenedor Docker no:

```bash
# Host → OK
curl http://192.168.100.85:18443/

# Docker → falla con "Failed to connect to server"
docker run --rm curlimages/curl http://192.168.100.85:18443/
```

---

## macOS (Docker Desktop)

Docker Desktop en Mac corre en una VM interna — **no hay `/proc` ni `iptables`**.
Los contenedores sí pueden alcanzar la LAN, pero el firewall de la PC-Coord puede estar rechazando conexiones desde la subred Docker (172.17.0.0/16 o 172.18.0.0/16).

### 1. Ver qué IP usa el contenedor

```bash
docker run --rm alpine ip route
# La IP de salida es algo como 172.17.0.1 o 172.18.0.1
```

### 2. En la PC-Coord (Linux), permitir esa subred

```bash
sudo ufw allow from 172.17.0.0/16 to any port 18443
sudo ufw allow from 172.18.0.0/16 to any port 18443
```

### 3. Verificar

```bash
docker run --rm curlimages/curl http://192.168.100.85:18443/
# Una respuesta HTTP (aunque sea 401) confirma conectividad
```

### Si sigue fallando: reiniciar Docker Desktop

```
Docker Desktop → Quit Docker Desktop → volver a abrir
```

A veces la VM pierde la ruta a la LAN y un reinicio la restaura.

---

## Linux

**Causa:** falta IP forwarding y/o regla MASQUERADE en iptables.

### Solución rápida — reiniciar Docker

```bash
sudo systemctl restart docker
docker run --rm curlimages/curl http://192.168.100.85:18443/
```

### Solución completa

```bash
# 1. Verificar IP forwarding (debe ser 1)
cat /proc/sys/net/ipv4/ip_forward

# 2. Habilitarlo si es 0
sudo sysctl -w net.ipv4.ip_forward=1

# 3. Ver la interfaz de red (eth0, wlan0, enp3s0…)
ip route | grep default
# Ejemplo: default via 192.168.100.1 dev eth0 ...
#                                         ^^^^

# 4. Añadir regla MASQUERADE (sustituir eth0 por la interfaz real)
sudo iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

# 5. Verificar
docker run --rm curlimages/curl http://192.168.100.85:18443/
```

### Hacer los cambios permanentes

```bash
sudo apt-get install -y iptables-persistent
sudo netfilter-persistent save
echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf
```

---

## Levantar el participante después del fix

```bash
# Sustituir 192.168.100.85 por la IP real del coordinador
BITCOIND_HOST=192.168.100.85 docker compose -f deploy/participant.yml up --build -d

# Verificar que el nodo CLN arrancó correctamente
docker compose -f deploy/participant.yml logs -f
```
