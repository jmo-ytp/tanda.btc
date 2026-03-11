# Tanda-BTC: Sequence Diagrams — Capa On-Chain

> **Nota:** Estos diagramas describen la capa on-chain del protocolo (Taproot + MuSig2 + HTLCs).
> El demo principal usa Lightning Network — ver [`sequence-diagrams-ln.md`](sequence-diagrams-ln.md).

Mermaid sequence diagrams for the full tanda protocol with 3 participants (P0, P1, P2).

---

## 1. Configuración inicial (Setup)

```mermaid
sequenceDiagram
    actor C as Coordinator
    actor P0
    actor P1
    actor P2

    Note over C: Deriva parámetros globales:<br/>n=3, amount=0.01 BTC<br/>t_refund=576 bloques<br/>winner_order=[0, 1, 2]

    loop Para k = 0, 1, 2
        C->>C: preimage_k = os.urandom(32)<br/>H_k = SHA256(preimage_k)
        C->>C: agg_pk = MuSig2_KeyAgg(P0, P1, P2)<br/>kac = apply_tweak(kac, taproot_tweak(...))<br/>assert kac.agg_pk == output_key_xonly
        C->>C: address_k = Taproot(<br/>  internal_key=agg_pk,<br/>  leaf1=[winner_k OP_CHECKSIGVERIFY OP_SHA256 H_k OP_EQUAL],<br/>  leaf2=[t_refund OP_CSV OP_DROP pk0 OP_CHECKSIG pk1 OP_CHECKSIGADD pk2 OP_CHECKSIGADD 2 OP_NUMEQUAL]<br/>)
    end

    Note over C: Distribuye a TODOS (público)
    C-->>P0: params + {address_k, H_k} para k=0,1,2
    C-->>P1: params + {address_k, H_k} para k=0,1,2
    C-->>P2: params + {address_k, H_k} para k=0,1,2

    Note over C: Envía preimage SOLO al ganador (confidencial)
    C--xP0: preimage_0 (ganador de ronda 0)
    C--xP1: preimage_1 (ganador de ronda 1)
    C--xP2: preimage_2 (ganador de ronda 2)
```

---

## 2. Ronda 0 — Cobro cooperativo (MuSig2 keypath)

P0 es el ganador. Todos cooperan. La ruta keypath de MuSig2 se usa para gastar.

```mermaid
sequenceDiagram
    actor C as Coordinator
    actor P0 as P0 (ganador)
    actor P1
    actor P2
    actor BTC as Bitcoin

    rect rgb(220, 240, 255)
        Note over C,BTC: Fase 1 — Contribuciones
        P0->>BTC: 0.01 BTC → address_0
        P1->>BTC: 0.01 BTC → address_0
        P2->>BTC: 0.01 BTC → address_0
        BTC->>BTC: Mina 1 bloque
        C->>BTC: scantxoutset("addr(address_0)")<br/>scriptPubKey devuelto como hex string
        BTC-->>C: 3 UTXOs confirmados (0.01 BTC cada uno)
    end

    rect rgb(220, 255, 220)
        Note over C,BTC: Fase 2 — Firma MuSig2 (keypath)
        C->>C: Construye claim_tx:<br/>3 inputs (UTXOs de P0, P1, P2) → P0_address<br/>amount = 0.03 BTC − fee
        C-->>P0: claim_tx
        C-->>P1: claim_tx
        C-->>P2: claim_tx

        loop Para cada input i = 0, 1, 2 (nonces frescos por input)
            Note over C,P2: Ronda 1 de nonces — intercambio de pub_nonces
            P0->>P0: (sec_nonce_0, pub_nonce_0) = nonce_gen(sk_0, pk_0, agg_pk)
            P1->>P1: (sec_nonce_1, pub_nonce_1) = nonce_gen(sk_1, pk_1, agg_pk)
            P2->>P2: (sec_nonce_2, pub_nonce_2) = nonce_gen(sk_2, pk_2, agg_pk)
            P0-->>C: pub_nonce_0
            P1-->>C: pub_nonce_1
            P2-->>C: pub_nonce_2

            C->>C: agg_nonce = NonceAgg(pn0, pn1, pn2)<br/>sighash_i = compute_taproot_sighash(claim_tx, inp_idx=i, utxos, SIGHASH_DEFAULT)<br/>session_ctx = SessionContext(agg_nonce, kac, sighash_i)
            C-->>P0: agg_nonce + session_ctx
            C-->>P1: agg_nonce + session_ctx
            C-->>P2: agg_nonce + session_ctx

            Note over C,P2: Ronda 2 de nonces — firmas parciales
            P0->>P0: psig_0 = partial_sign(sec_nonce_0, sk_0, session_ctx)
            P1->>P1: psig_1 = partial_sign(sec_nonce_1, sk_1, session_ctx)
            P2->>P2: psig_2 = partial_sign(sec_nonce_2, sk_2, session_ctx)
            P0-->>C: psig_0
            P1-->>C: psig_1
            P2-->>C: psig_2

            C->>C: final_sig_i = PartialSigAgg([psig0, psig1, psig2], session_ctx)<br/>g = 1 si Q.y par, else N−1<br/>s = Σs_i + e·g·tacc (mod n)<br/>claim_tx.inputs[i].witness = [final_sig_i]
        end

        C->>BTC: broadcast claim_tx
        BTC->>BTC: Mina 1 bloque
        BTC-->>P0: 0.03 BTC − fee (pot completo)
    end
```

---

## 3. Ronda 1 — Fallback HTLC (leaf1)

P1 es el ganador. P0 rechaza cooperar. Se usa la hoja HTLC (leaf1).

```mermaid
sequenceDiagram
    actor C as Coordinator
    actor P0
    actor P1 as P1 (ganador)
    actor P2
    actor BTC as Bitcoin

    rect rgb(220, 240, 255)
        Note over C,BTC: Fase 1 — Contribuciones
        P0->>BTC: 0.01 BTC → address_1
        P1->>BTC: 0.01 BTC → address_1
        P2->>BTC: 0.01 BTC → address_1
        BTC->>BTC: Mina 1 bloque
        C->>BTC: scantxoutset("addr(address_1)")
        BTC-->>C: 3 UTXOs confirmados
    end

    rect rgb(255, 235, 205)
        Note over C,BTC: Fase 2 — Intento cooperativo (falla)
        C->>C: Construye claim_tx (3 inputs → P1_address)
        C-->>P0: claim_tx — solicita pub_nonce
        C-->>P1: claim_tx — solicita pub_nonce
        C-->>P2: claim_tx — solicita pub_nonce

        alt P0 rechaza / está offline
            P0--xC: (sin respuesta)
            Note over C: Timeout — firma cooperativa fallida
        end
    end

    rect rgb(255, 220, 220)
        Note over C,BTC: Fase 3 — Fallback HTLC (leaf1, sin timelock)
        C--xP1: preimage_1 (confidencial, ya enviado en setup)
        Note over P1: P1 ya tiene preimage_1<br/>H_1 = SHA256(preimage_1)

        C->>P1: htlc_claim_tx + htlc_script + control_block para leaf1

        loop Para cada input i = 0, 1, 2
            P1->>P1: sighash_i = tapscript_sighash(htlc_claim_tx, inp_idx=i, utxos, htlc_script)<br/>sig_i = schnorr_sign(sk_1, sighash_i)
            P1->>P1: witness_i = [sig_i, preimage_1, htlc_script, control_block]<br/>htlc_claim_tx.inputs[i].witness = witness_i
        end

        P1->>BTC: broadcast htlc_claim_tx
        BTC->>BTC: Verifica: sig válida ∧ SHA256(preimage_1) == H_1<br/>Mina 1 bloque
        BTC-->>P1: 0.03 BTC − fee (pot completo)
    end
```

---

## 4. Ronda 2 — Reembolso colectivo (leaf2)

P2 es el ganador designado pero desaparece. P0 y P1 recuperan sus fondos tras el timelock CSV.

```mermaid
sequenceDiagram
    actor C as Coordinator
    actor P0
    actor P1
    actor P2 as P2 (desaparece)
    actor BTC as Bitcoin

    rect rgb(220, 240, 255)
        Note over C,BTC: Fase 1 — Contribuciones
        P0->>BTC: 0.01 BTC → address_2
        P1->>BTC: 0.01 BTC → address_2
        P2->>BTC: 0.01 BTC → address_2
        BTC->>BTC: Mina 1 bloque
        C->>BTC: scantxoutset("addr(address_2)")
        BTC-->>C: 3 UTXOs confirmados
    end

    rect rgb(255, 235, 205)
        Note over C,BTC: Fase 2 — Intento cooperativo (falla)
        C->>C: Construye claim_tx (3 inputs → P2_address)
        C-->>P0: claim_tx — solicita pub_nonce
        C-->>P1: claim_tx — solicita pub_nonce
        C-->>P2: claim_tx — solicita pub_nonce

        alt P2 no responde
            P2--xC: (sin respuesta)
            Note over C: Timeout — firma cooperativa fallida
        end
    end

    rect rgb(240, 240, 240)
        Note over C,BTC: Fase 3 — Esperar timelock CSV
        C->>BTC: Mine 576 bloques
        Note over BTC: 576 bloques ≈ 4 días<br/>Timelock leaf2 (t_refund=576) expira
    end

    rect rgb(220, 255, 220)
        Note over C,BTC: Fase 4 — Reembolso colectivo (leaf2)
        C->>C: Construye refund_tx:<br/>3 inputs → [P0_addr, P1_addr, P2_addr]<br/>output_i = 0.01 BTC − fee/3
        C-->>P0: refund_tx + refund_script + control_block
        C-->>P1: refund_tx + refund_script + control_block

        loop Para cada input i = 0, 1, 2
            P0->>P0: sig_0 = sign_tapscript(refund_tx, inp_idx=i, utxos, sk_0, refund_script)
            P1->>P1: sig_1 = sign_tapscript(refund_tx, inp_idx=i, utxos, sk_1, refund_script)
            P0-->>C: sig_0
            P1-->>C: sig_1

            Note over C: sigs ordenadas por pubkeys (ascendente)<br/>P2 ausente → b"" en su lugar<br/>witness = reversed([sig_0, sig_1, b""])<br/>       = [b"", sig_1, sig_0, refund_script, control_block]
            C->>C: refund_tx.inputs[i].witness = [b"", sig_1, sig_0, refund_script, control_block]
        end

        C->>BTC: broadcast refund_tx (P0 o P1 puede emitirlo)
        BTC->>BTC: Verifica: 576 bloques pasados ∧ 2-de-3 firmas válidas<br/>Mina 1 bloque
        BTC-->>P0: 0.01 BTC − fee/3
        BTC-->>P1: 0.01 BTC − fee/3
        BTC-->>P2: 0.01 BTC − fee/3
    end
```

---

## 5. Vista general — Ciclo completo

Tres rondas, tres ganadores. Cada participante recibe el pot exactamente una vez.

```mermaid
sequenceDiagram
    actor C as Coordinator
    actor P0
    actor P1
    actor P2
    actor BTC as Bitcoin

    rect rgb(220, 240, 255)
        Note over C,BTC: RONDA 0 — P0 gana (MuSig2 keypath cooperativo)
        P0->>BTC: 0.01 BTC → address_0
        P1->>BTC: 0.01 BTC → address_0
        P2->>BTC: 0.01 BTC → address_0
        BTC->>BTC: +1 bloque
        Note over C,P2: MuSig2: 2 rondas de nonces × 3 inputs<br/>claim_tx firmada colectivamente
        C->>BTC: broadcast claim_tx (keypath)
        BTC->>BTC: +1 bloque
        BTC-->>P0: 0.03 BTC − fee ✓
    end

    rect rgb(220, 255, 220)
        Note over C,BTC: RONDA 1 — P1 gana (HTLC leaf1, P0 rechaza cooperar)
        P0->>BTC: 0.01 BTC → address_1
        P1->>BTC: 0.01 BTC → address_1
        P2->>BTC: 0.01 BTC → address_1
        BTC->>BTC: +1 bloque
        Note over C,P2: MuSig2 falla (P0 offline)<br/>P1 usa preimage_1 + sk_1 → htlc_claim_tx
        P1->>BTC: broadcast htlc_claim_tx (leaf1)
        BTC->>BTC: +1 bloque
        BTC-->>P1: 0.03 BTC − fee ✓
    end

    rect rgb(255, 235, 205)
        Note over C,BTC: RONDA 2 — Reembolso (leaf2, P2 desaparece)
        P0->>BTC: 0.01 BTC → address_2
        P1->>BTC: 0.01 BTC → address_2
        P2->>BTC: 0.01 BTC → address_2
        BTC->>BTC: +1 bloque
        Note over C,P2: MuSig2 falla (P2 offline)<br/>Esperar t_refund=576 bloques
        BTC->>BTC: +576 bloques
        Note over C,P2: P0 + P1 firman refund_tx (2-de-3, leaf2)
        C->>BTC: broadcast refund_tx (leaf2)
        BTC->>BTC: +1 bloque
        BTC-->>P0: 0.01 BTC − fee/3 ✓
        BTC-->>P1: 0.01 BTC − fee/3 ✓
        BTC-->>P2: 0.01 BTC − fee/3 ✓
    end

    Note over C,BTC: Ciclo completo:<br/>R0 → P0 ganó el pot<br/>R1 → P1 ganó el pot<br/>R2 → todos recuperaron su aporte<br/>Coordinator = trustless (cualquier participante puede serlo)
```
