# Tanda-BTC: Diagramas de Secuencia — Lightning Network

Diagramas Mermaid del protocolo tanda sobre Lightning Network con 3 participantes (P0, P1, P2).
El coordinador usa **hold invoices** (BoltzExchange/hold plugin para CLN) para preservar la
garantía trustless.

---

## 1. Bootstrap: topología de canales

El coordinador abre un canal hacia cada participante con push de liquidez.
Ocurre una sola vez antes de las rondas.

```mermaid
sequenceDiagram
    actor BC as Bitcoin Core<br/>(regtest)
    actor C  as Coordinador<br/>(CLN node)
    actor AP0 as API P0<br/>(:8080)
    actor AP1 as API P1<br/>(:8081)
    actor AP2 as API P2<br/>(:8082)

    Note over BC: mine(101) → bloques maduros

    BC-->>C: N × 200k sat + fee<br/>(funding TX on-chain → cln_coord_addr)<br/>para N=3: ~0.0061 BTC

    Note over C: wait_cln_funds() — espera UTXOs confirmados

    C->>AP0: GET /node_info
    AP0-->>C: {id: "03aabb...", address: {host, port}}
    C->>AP1: GET /node_info
    AP1-->>C: {id: "03ccdd...", address: {host, port}}
    C->>AP2: GET /node_info
    AP2-->>C: {id: "03eeff...", address: {host, port}}

    C->>C: cln.connect("03aabb...", host_p0, 9735)
    C->>C: cln.fund_channel("03aabb...", 200k sat, push_msat=150,000,000)
    Note over C: Canal coord→P0 abierto<br/>Coord: 50k sat local | P0: 150k sat local
    BC-->>BC: mine(1) → change UTXO confirmado<br/>(CLN solo gasta UTXOs confirmados)

    C->>C: cln.connect("03ccdd...", host_p1, 9735)
    C->>C: cln.fund_channel("03ccdd...", 200k sat, push_msat=150,000,000)
    BC-->>BC: mine(1) → change UTXO confirmado

    C->>C: cln.connect("03eeff...", host_p2, 9735)
    C->>C: cln.fund_channel("03eeff...", 200k sat, push_msat=150,000,000)

    BC-->>BC: mine(6) → 6 confirmaciones → CHANNELD_NORMAL

    Note over C,AP2: Bootstrap completo.<br/>Cada participante tiene 150k sat outbound<br/>— suficiente para N rondas de 10k sat.
```

---

## 2. Ronda completa: camino feliz (happy path)

El coordinador genera **N preimages distintas** (una por participante) porque CLN rechaza
múltiples hold invoices con el mismo `payment_hash`.

Las llamadas `POST /pay_invoice` se ejecutan en un `ThreadPoolExecutor` — son **no bloqueantes**
para el coordinador, que sigue ejecutando `wait_all_accepted` mientras los threads corren.

```mermaid
sequenceDiagram
    actor C  as Coordinador
    actor P0 as API P0
    actor P1 as API P1
    actor P2 as API P2 (ganador)

    Note over C: preimage_i = os.urandom(32) × 3<br/>payment_hash_i = SHA256(preimage_i)<br/>Ganador esta ronda: P2

    C->>C: cln.holdinvoice(H_0, amount=10,000,000 msat) → bolt11_0
    C->>C: cln.holdinvoice(H_1, amount=10,000,000 msat) → bolt11_1
    C->>C: cln.holdinvoice(H_2, amount=10,000,000 msat) → bolt11_2

    Note over C: ThreadPoolExecutor.submit() × 3<br/>(no bloqueante — threads corren en paralelo)

    C-)P0: POST /pay_invoice {bolt11_0}
    C-)P1: POST /pay_invoice {bolt11_1}
    C-)P2: POST /pay_invoice {bolt11_2}

    Note over P0: cln.pay(bolt11_0) — bloqueante<br/>HTLC_0 se bloquea en el nodo coord
    Note over P1: cln.pay(bolt11_1) — bloqueante<br/>HTLC_1 se bloquea en el nodo coord
    Note over P2: cln.pay(bolt11_2) — bloqueante<br/>HTLC_2 se bloquea en el nodo coord

    loop wait_all_accepted (poll cada 1s, timeout=120s)
        C->>C: get_incoming_htlc_hashes()<br/>vía listpeerchannels()
        C->>C: ¿los 3 payment_hashes en HTLCs direction=in?
    end

    Note over C: ✓ 3/3 HTLCs aceptados

    C->>P2: POST /create_invoice {amount_msat: 30,000,000, label: "pot-round-k"}
    P2-->>C: {bolt11: bolt11_pot}

    C->>C: cln.pay(bolt11_pot)<br/>P2 recibe 30k sat ANTES de que coord recupere nada

    Note over C: Settle — revela preimages en orden
    C->>C: cln.settle_holdinvoice(preimage_0.hex())
    C->>C: cln.settle_holdinvoice(preimage_1.hex())
    C->>C: cln.settle_holdinvoice(preimage_2.hex())

    Note over P0: update_fulfill_htlc recibido<br/>cln.pay() retorna → thread libera
    P0-->>C: 200 OK {payment_hash}
    Note over P1: update_fulfill_htlc recibido
    P1-->>C: 200 OK {payment_hash}
    Note over P2: update_fulfill_htlc recibido
    P2-->>C: 200 OK {payment_hash}

    Note over C: fut.result() × 3 → ronda completa
    Note over C,P2: P2 neto: +20k sat (ganó 30k, pagó 10k)<br/>P0, P1 neto: −10k sat cada uno
```

---

## 3. Garantía trustless: flujo de HTLCs

Por qué el protocolo es trustless: los fondos de los participantes solo se liberan
**después** de que el coordinador pague al ganador.

```mermaid
sequenceDiagram
    actor P0 as P0
    actor C  as Coordinador
    actor P2 as P2 (ganador)

    Note over P0,P2: Los 3 HTLCs están bloqueados<br/>en el nodo del coordinador (estado ACCEPTED)

    rect rgb(255, 245, 200)
        Note over C: SI el coordinador NO paga al ganador...
        Note over P0,P2: Los hold invoices expiran por CLTV<br/>(timeout configurado por CLN)
        Note over P0: HTLC devuelto automáticamente<br/>P0 recupera sus 10k sat
        Note over P2: HTLC devuelto automáticamente<br/>P2 recupera sus 10k sat
    end

    rect rgb(220, 255, 220)
        Note over C: SI el coordinador paga al ganador...
        C->>P2: cln.pay(bolt11_pot) → 30k sat
        Note over P2: P2 recibe el bote ANTES<br/>de que el coordinador reciba nada
        C->>C: settle_holdinvoice(preimage_i) × 3<br/>→ coord recupera 3 × 10k sat<br/>SOLO DESPUÉS de haber pagado al ganador
    end
```

**Invariante:** El coordinador solo recupera su liquidez si primero pagó al ganador.
Si no paga, los HTLCs expiran y los participantes recuperan sus sats sin pérdida.

---

## 4. Fallback: timeout de participante

Si no todos los participantes pagan dentro del plazo, `wait_all_accepted` lanza
`TimeoutError` y la ronda aborta. El coordinador **no llama activamente** a
`cancel_holdinvoice` — los hold invoices expiran por CLTV automáticamente.

```mermaid
sequenceDiagram
    actor C  as Coordinador
    actor P0 as P0 (pagó)
    actor P1 as P1 (no pagó)
    actor P2 as P2 (no pagó)

    C->>C: holdinvoice(H_0) → bolt11_0
    C->>C: holdinvoice(H_1) → bolt11_1
    C->>C: holdinvoice(H_2) → bolt11_2

    C-)P0: POST /pay_invoice {bolt11_0}
    C-)P1: POST /pay_invoice {bolt11_1}
    C-)P2: POST /pay_invoice {bolt11_2}

    Note over P0: HTLC_0 bloqueado en coord

    loop wait_all_accepted (cada 1s, timeout=120s)
        C->>C: get_incoming_htlc_hashes()
        C->>C: 1/3 HTLCs aceptados — sigue esperando
    end

    Note over C: TimeoutError: solo 1/3 HTLCs aceptados<br/>run_round_ln() lanza excepción — ronda abortada

    Note over P0,P2: Los hold invoices expiran por CLTV<br/>(no hay cancel_holdinvoice explícito)
    Note over P0: P0 recupera sus 10k sat<br/>cuando el HTLC expira
```

> **Nota de implementación:** `CLNRpc.cancel_holdinvoice()` existe pero no está conectado
> al flujo actual. Si se necesita cancelación activa e inmediata, llamar a
> `cancel_holdinvoice(payment_hash)` por cada invoice antes de abortar.

---

## 5. Topología de red y flujo de sats por ronda

Vista estática de canales y flujo de valor en una ronda donde P2 gana.

```mermaid
flowchart LR
    subgraph LN["Red Lightning (regtest)"]
        direction TB
        C["Coordinador\n(CLN node)\n50k sat local\npor canal"]

        C -- "canal 200k sat\npush 150k → P0" --- P0["P0\n150k sat outbound"]
        C -- "canal 200k sat\npush 150k → P1" --- P1["P1\n150k sat outbound"]
        C -- "canal 200k sat\npush 150k → P2" --- P2["P2\n150k sat outbound"]
    end

    subgraph Round["Flujo de sats en la ronda (P2 gana)"]
        direction LR
        S0["P0 paga\n10k sat"] --> SC["Coord retiene\n30k sat\n(HTLCs ACCEPTED)"]
        S1["P1 paga\n10k sat"] --> SC
        S2["P2 paga\n10k sat"] --> SC
        SC --> SW["P2 recibe\n30k sat\n(bote)"]
        SW --> SS["Coord settle:\nrecupera 30k sat\n(net = 0)"]
    end
```

---

## 6. Comparación: on-chain vs Lightning

| | On-chain | Lightning |
|---|---|---|
| Fee por aportación | ~2,000 sat (varias TXs on-chain) | ~0–10 sat (pagos off-chain) |
| Tiempo de liquidación | ~10 min por confirmación | instantáneo |
| Garantía trustless | Taproot HTLC + CSV timelock | Hold invoices + expiración CLTV |
| Privacidad | Direcciones y montos públicos en blockchain | Off-chain; solo apertura/cierre de canal visible |
| Complejidad técnica | Taproot + MuSig2 + BIP-341/342 | CLN + BoltzExchange/hold plugin |
| Requiere nodo LN | No | Sí (CLN con hold plugin) |
| Fallback si coord desaparece | Reclamar via leaf1 (HTLC) o leaf2 (refund CSV) | HTLCs expiran por CLTV → fondos devueltos |

El protocolo LN preserva la garantía trustless del diseño on-chain original:
los participantes solo pierden sus sats si el coordinador cumple su parte.
