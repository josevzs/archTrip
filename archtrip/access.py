"""Quién está mirando: la red de casa o el mundo.

La app se publica por dos sitios a la vez: en la red local, con su nombre interno a través del
proxy, y hacia fuera por un túnel (Tailscale Funnel) que entra directo al puerto. Un viaje
marcado como privado solo existe para el primero: desde fuera no se lista, no se abre y no se
exporta, como si no estuviera. Así se puede preparar una variante y publicarla cuando toque.

Se distingue por el nombre con el que piden la página (`Host`), que es lo único que diferencia
las dos entradas; `ARCHTRIP_PUBLIC_HOSTS` permite cambiar la lista (separada por comas)."""
import os

from flask import request

DEFAULT_PUBLIC_HOSTS = ".ts.net"


def public_hosts():
    raw = os.environ.get("ARCHTRIP_PUBLIC_HOSTS", DEFAULT_PUBLIC_HOSTS)
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def is_public_request():
    """True si la petición llega desde fuera (el túnel), no desde la red de casa."""
    host = (request.host or "").split(":")[0].lower()
    return any(host.endswith(pattern) or host == pattern.lstrip(".") for pattern in public_hosts())
