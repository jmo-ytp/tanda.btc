# Tanda-BTC: Diagramas de Secuencia — Lightning Network

Diagramas Mermaid del protocolo tanda sobre Lightning Network con 3 participantes (P0, P1, P2).
El coordinador usa **hold invoices** (HTLCs manuales) de CLN para preservar la garantía trustless.

---

## 1. Bootstrap: topología de canales

El coordinador abre un canal hacia cada participante y hace **push** de sats para darles
capacidad de salida. Esto ocurre una sola vez antes de las rondas.

```mermaid
sequenceDiagram
    actor BC as Bitcoin Core<br/>(regtest)
    actor C  as Coordinador<br/>(CLN node)
    actor P0 as P0 CLN node
    actor P1 as P1 CLN node
    actor P2 as P2 CLN node

    Note over BC: mine(101) → bloque maduros

    BC-->>C: 200k sat (funding TX on-chain)

    C->>P0: connect(node_id, "cln-p0", 9735)
    C->>P0: fund_channel(200k sat, push=150k sat)
    Note over C,P0: Canal: coord→p0<br/>Coord: 50k sat local<br/>P0: 150k sat local
    BC-->>BC: mine(1) → confirma change UTXO<br/>(CLN solo gasta UTXOs confirmados)

    C->>P1: connect(node_id, "cln-p1", 9735)
    C->>P1: fund_channel(200k sat, push=150k sat)
    BC-->>BC: mine(1) → confirma change UTXO

    C->>P2: connect(node_id, "cln-p2", 9735)
    C->>P2: fund_channel(200k sat, push=150k sat)

    BC-->>BC: mine(6) → 6 confirmaciones para CHANNELD_NORMAL

    Note over C,P2: Todos los canales llegan a<br/>estado CHANNELD_NORMAL
```

**Resultado:** Cada participante tiene 150k sat de capacidad de salida hacia el coordinador
— suficiente para pagar N rondas de 10k sat cada una.

---

## 2. Ronda completa: camino feliz (happy path)

El coordinador usa **N preimages distintas** (una por participante) porque CLN rechaza
múltiples hold invoices con el mismo `payment_hash`.

```mermaid
sequenceDiagram
    actor C  as Coordinador
    actor P0 as API P0
    actor P1 as API P1
    actor P2 as API P2 (ganador)

    Note over C: preimage_i = os.urandom(32) × 3<br/>payment_hash_i = SHA256(preimage_i)<br/>Ganador esta ronda: P2<br/><br/>Preimages quedan SECRET con el coordinador

    C->>C: holdinvoice(payment_hash=H_0, amount=10k sat)<br/>→ bolt11_0

    C->>C: holdinvoice(payment_hash=H_1, amount=10k sat)<br/>→ bolt11_1

    C->>C: holdinvoice(payment_hash=H_2, amount=10k sat)<br/>→ bolt11_2

    par Distribuir bolt11 y cobrar en paralelo
        C->>P0: POST /pay_invoice {bolt11_0}
        Note over P0: cln.pay(bolt11_0)<br/>[BLOQUEANTE hasta settle/cancel]
    and
        C->>P1: POST /pay_invoice {bolt11_1}
        Note over P1: cln.pay(bolt11_1)<br/>[BLOQUEANTE hasta settle/cancel]
    and
        C->>P2: POST /pay_invoice {bolt11_2}
        Note over P2: cln.pay(bolt11_2)<br/>[BLOQUEANTE hasta settle/cancel]
    end

    Note over P0,C: HTLC_0 bloqueado en coord<br/>(update_add_htlc → P0 debita 10k sat)
    Note over P1,C: HTLC_1 bloqueado en coord
    Note over P2,C: HTLC_2 bloqueado en coord

    loop Poll vía listpeerchannels (cada 0.5s)
        C->>C: get_incoming_htlc_hashes()
        C->>C: ¿los 3 payment_hashes están<br/>en HTLCs entrantes?
    end

    Note over C: ✓ Los 3 HTLCs están aceptados

    C->>P2: POST /create_invoice {amount=30k sat}
    P2-->>C: bolt11_pot

    C->>C: cln.pay(bolt11_pot)<br/>→ P2 recibe 30k sat (el bote)

    Note over C: Settle en orden — cada settleholdinvoice<br/>revela la preimage que el coordinador guardó en secreto

    C->>C: settleholdinvoice(preimage_0)<br/>→ plugin deriva H_0=SHA256(P_0), liquida HTLC

    C->>C: settleholdinvoice(preimage_1)

    C->>C: settleholdinvoice(preimage_2)

    Note over P0: update_fulfill_htlc recibido<br/>cln.pay() retorna OK
    P0-->>C: 200 OK {payment_hash}

    Note over P1: update_fulfill_htlc recibido
    P1-->>C: 200 OK {payment_hash}

    Note over P2: update_fulfill_htlc recibido<br/>cln.pay() retorna OK
    P2-->>C: 200 OK {payment_hash}

    Note over C,P2: Ronda completa.<br/>P2 neto: +20k sat (ganó 30k, pagó 10k).<br/>P0, P1 neto: -10k sat cada uno.
```

---

## 3. Garantía trustless: flujo de HTLCs

Este diagrama muestra por qué el protocolo es trustless: los fondos de los participantes
solo se liberan **después** de que el coordinador paga al ganador.

```mermaid
sequenceDiagram
    actor P0 as P0
    actor C  as Coordinador
    actor P2 as P2 (ganador)

    Note over P0,P2: Los 3 HTLCs están bloqueados<br/>en el nodo del coordinador

    rect rgb(255, 245, 200)
        Note over C: SI el coordinador NO paga al ganador...
        Note over P0,P2: Los hold invoices expiran<br/>(cltv=40 bloques alcanzado)
        C-->>P0: update_fail_htlc (HTLC expirado)
        C-->>P2: update_fail_htlc (HTLC expirado)
        Note over P0: P0 recupera sus 10k sat<br/>automáticamente
        Note over P2: P2 recupera sus 10k sat<br/>automáticamente
    end

    rect rgb(220, 255, 220)
        Note over C: SI el coordinador paga al ganador...
        C->>P2: pay(bolt11_pot) → 30k sat
        Note over P2: P2 recibe el bote ANTES<br/>de que el coordinador reciba nada
        C->>C: settleholdinvoice(preimage_i) × 3<br/>plugin deriva H_i=SHA256(P_i) internamente
        Note over C: El coordinador recupera 3 × 10k sat<br/>SOLO DESPUÉS de pagar al ganador
    end
```

**Invariante:** El coordinador solo recupera su liquidez si primero pagó al ganador.
Si no paga, los HTLCs expiran y los participantes recuperan sus sats sin pérdida.

---

## 4. Fallback: cancelación antes del pago

Si el coordinador detecta que no todos los participantes pagaron a tiempo,
cancela todos los hold invoices.

```mermaid
sequenceDiagram
    actor C  as Coordinador
    actor P0 as P0 (pagó)
    actor P1 as P1 (no pagó)
    actor P2 as P2 (no pagó)

    C->>C: holdinvoice(payment_hash=H_0, amount=10k sat) → bolt11_0
    C->>C: holdinvoice(payment_hash=H_1, amount=10k sat) → bolt11_1
    C->>C: holdinvoice(payment_hash=H_2, amount=10k sat) → bolt11_2

    C->>P0: POST /pay_invoice {bolt11_0}
    Note over P0: HTLC_0 bloqueado en coord

    Note over C: Timeout: P1 y P2 no pagaron<br/>dentro del plazo (t_claim bloques)

    C->>C: cancel_holdinvoice(payment_hash_0)
    Note over C: update_fail_htlc enviado a P0

    C->>C: cancel_holdinvoice(payment_hash_1)
    C->>C: cancel_holdinvoice(payment_hash_2)

    Note over P0: P0 recupera sus 10k sat<br/>(HTLC devuelto)
    P0-->>C: pay() retorna error (cancelled)

    Note over C,P2: Nadie pierde nada.<br/>Ronda abortada limpiamente.
```

---

## 5. Topología de red y flujo de sats por ronda

Vista estática de canales y flujo de valor en una ronda donde P2 gana.

```mermaid
flowchart LR
    subgraph LN["Red Lightning (regtest)"]
        direction TB
        C["Coordinador\n(CLN node)\n50k sat local"]

        C -- "canal 200k sat\npush 150k → P0" --- P0["P0\n150k sat local\n(outbound)"]
        C -- "canal 200k sat\npush 150k → P1" --- P1["P1\n150k sat local\n(outbound)"]
        C -- "canal 200k sat\npush 150k → P2" --- P2["P2\n150k sat local\n(outbound)"]
    end

    subgraph Round["Flujo de sats en la ronda (P2 gana)"]
        direction LR
        S0["P0 paga\n10k sat →"] --> SC["Coord\nretiene\n30k sat\n(HTLCs)"]
        S1["P1 paga\n10k sat →"] --> SC
        S2["P2 paga\n10k sat →"] --> SC
        SC --> SW["→ P2 recibe\n30k sat\n(el bote)"]
        SW --> SS["Coord settle:\nrecupera 30k sat"]
    end
```

---

## 6. Comparación: on-chain vs Lightning

| | On-chain | Lightning |
|---|---|---|
| Fee por aportación | ~2,000 sat (20% de 10k sat) | ~0–10 sat (<0.1%) |
| Tiempo de confirmación | ~10 min por bloque | instantáneo |
| Garantía trustless | Taproot HTLC + CSV | Hold invoices + expiración CLTV |
| Privacidad | Pública en blockchain | Off-chain, solo canal visible |
| Complejidad técnica | Taproot + MuSig2 | CLN + holdinvoice plugin |
| Requiere nodo LN | No | Sí (CLN con holdinvoice plugin) |

El protocolo LN preserva la garantía trustless del diseño on-chain original:
los participantes solo pierden sus sats si el coordinador cumple su parte.
