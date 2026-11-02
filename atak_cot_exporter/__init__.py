def classFactory(iface):
    from .plugin import ATAKPlugin
    return ATAKPlugin(iface)
