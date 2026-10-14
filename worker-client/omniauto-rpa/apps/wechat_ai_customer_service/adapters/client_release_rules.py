"""Pure transport/channel contract shared by publisher, server and updater."""
CHANNEL_FOR_TRANSPORT = {'ocr': 'gray', 'hook': 'gray-hook'}
SUPPORTED_CHANNELS = frozenset(CHANNEL_FOR_TRANSPORT.values())
HOOK_MINIMUM_UPDATER_VERSION = '0.10.1'


def channel_for_transport(transport):
    if transport not in CHANNEL_FOR_TRANSPORT:
        raise ValueError('UPDATE_TRANSPORT_SELECTION_INVALID')
    return CHANNEL_FOR_TRANSPORT[transport]


def transport_for_channel(channel):
    for transport, candidate in CHANNEL_FOR_TRANSPORT.items():
        if candidate == channel:
            return transport
    raise ValueError('UPDATE_CHANNEL_UNSUPPORTED')


def compatible_package(manifest, channel):
    expected = transport_for_channel(channel)
    # Previously signed gray packages omit both fields and remain OCR-only.
    transport = manifest.get('transport', 'ocr')
    package_channel = manifest.get('channel', 'gray')
    return transport == expected and package_channel == channel
