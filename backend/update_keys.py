"""Public keys trusted for signed update manifests (Ed25519, base64 of the 32 raw bytes).

The matching private key is the GitHub Actions secret CLIPFACTORY_UPDATE_KEY (never committed).
Key rotation: add the new public key here, ship a release signed with the OLD key, then switch the
secret to the new key; remove the old key a few releases later. See RELEASING.md.
"""
PUBLIC_KEYS = [
    "7X+sEDbaX2BTSIZOuOIcSnuXiAf5IwSwCx3ICvoihbU=",
]
