# Multi-PC Troubleshooting: Docker sin acceso a la LAN

## Síntoma

Desde el host funciona, pero desde dentro de un contenedor Docker no:

```bash
# Host → OK
curl http://192.168.100.85:18443/

# Docker → falla con "Failed to connect to server"
docker run --rm curlimages/curl http://192.168.100.85:18443/
```

El nodo CLN lo reporta así en sus logs:

```
RPC connection timed out. Could not connect to bitcoind using bitcoin-cli.
**BROKEN** plugin-bcli: RPC connection timed out.
```

---

## Diagnóstico rápido (cualquier OS participante)

```bash
# Ver qué IP/subred usa Docker en este equipo
docker run --rm alpine ip route

# Probar conectividad directamente desde un contenedor
docker run --rm curlimages/curl -v http://<IP-coord>:18443/
# Una respuesta HTTP (aunque sea 401) = conexión OK
# "Failed to connect" = el coordinador está bloqueando esa subred
```

---

## PC-Coord: Parrot OS (coordinador)

Parrot OS usa `nftables` por defecto (no `ufw`). Hay que permitir las subredes Docker
de los participantes en el puerto 18443.

### Ver qué reglas están activas

```bash
sudo nft list ruleset
# o si tiene iptables legacy:
sudo iptables -L INPUT -n --line-numbers
```

### Abrir puerto 18443 para subredes Docker de participantes

```bash
# nftables (Parrot OS por defecto)
sudo nft add rule inet filter input ip saddr 172.16.0.0/12 tcp dport 18443 accept

# Si usa iptables legacy:
sudo iptables -I INPUT -s 172.16.0.0/12 -p tcp --dport 18443 -j ACCEPT
```

`172.16.0.0/12` cubre todo el rango que Docker usa (172.17–172.31.x.x).

### Hacer permanente (nftables)

```bash
sudo nft list ruleset > /etc/nftables.conf
sudo systemctl enable nftables
```

### Verificar desde el participante

```bash
docker run --rm curlimages/curl http://192.168.100.85:18443/
```

---

## Participante: macOS — Ivan (Docker Desktop)

Docker Desktop en Mac corre en una VM interna — **no hay `/proc` ni `iptables`**.
Los contenedores sí pueden alcanzar la LAN, pero el firewall de la PC-Coord puede
rechazar conexiones desde la subred Docker (172.x.x.x).

### 1. Ver qué subred usa Docker

```bash
docker run --rm alpine ip route
# Busca: default via 172.X.X.1 dev eth0 src 172.X.X.X
```

### 2. Abrir esa subred en la PC-Coord (ver sección Parrot OS arriba)

La regla `172.16.0.0/12` ya cubre todas las subredes Docker posibles, así que
si se aplicó en la PC-Coord no hace falta nada más aquí.

### 3. Si sigue fallando: reiniciar Docker Desktop

```
Docker Desktop → Quit Docker Desktop → volver a abrir
```

La VM a veces pierde la ruta a la LAN; un reinicio la restaura.

### 4. Verificar

```bash
docker run --rm curlimages/curl http://192.168.100.85:18443/
```

---

## Participante: Arch Linux (checkin)

Arch no instala firewall por defecto, pero puede tener `iptables` o `nftables` activos.

### Verificar IP forwarding y MASQUERADE

```bash
# Debe ser 1
cat /proc/sys/net/ipv4/ip_forward

# Habilitar si es 0
sudo sysctl -w net.ipv4.ip_forward=1

# Ver interfaz de salida
ip route | grep default
# Ejemplo: default via 192.168.100.1 dev enp3s0 ...
#                                         ^^^^^^

# Añadir MASQUERADE (sustituir enp3s0 por la interfaz real)
sudo iptables -t nat -A POSTROUTING -o enp3s0 -j MASQUERADE
```

### Si usa nftables (systemd-networkd + nftables)

```bash
sudo nft add table nat
sudo nft add chain nat postrouting { type nat hook postrouting priority 100 \; }
sudo nft add rule nat postrouting oifname "enp3s0" masquerade
```

### Solución rápida — reiniciar Docker

```bash
sudo systemctl restart docker
docker run --rm curlimages/curl http://192.168.100.85:18443/
```

### Hacer permanente en Arch

```bash
# IP forwarding
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-docker.conf
sudo sysctl --system

# iptables-nft (si usa iptables sobre nftables)
sudo pacman -S --needed iptables-nft
sudo systemctl enable iptables
sudo iptables-save | sudo tee /etc/iptables/iptables.rules
```

---

## Levantar el participante después del fix

```bash
# Sustituir 192.168.100.85 por la IP real del coordinador
BITCOIND_HOST=192.168.100.85 docker compose -f deploy/participant.yml up --build -d

# Verificar logs del CLN
docker compose -f deploy/participant.yml logs -f cln-participant

# Verificar que la API responde
curl http://localhost:8080/health
```
