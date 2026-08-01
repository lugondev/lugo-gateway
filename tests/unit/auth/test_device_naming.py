from app.services.auth.device_naming import default_device_name


def test_derives_the_ap_name_the_device_shows_during_setup():
    # The firmware builds its setup-AP SSID as "Lugo-%02X%02X" of the MAC's last
    # two bytes, and sends the same MAC as a 12-hex-char serial at pair/init.
    # Verified against real hardware: serial 2884855048d0 -> AP "Lugo-48D0".
    assert default_device_name("2884855048d0") == "Lugo-48D0"


def test_uppercases_hex_so_it_matches_the_ssid_exactly():
    assert default_device_name("aabbccddeeff") == "Lugo-EEFF"


def test_already_uppercase_serial_is_left_alone():
    assert default_device_name("AABBCCDDEEFF") == "Lugo-EEFF"


def test_short_serial_falls_back_rather_than_emitting_a_fragment():
    # Real hardware never does this, but `serial` is a free-form string on the
    # wire. A truncated "Lugo-ab" would look like a real AP name and isn't one.
    assert default_device_name("abc") == "New device"
    assert default_device_name("") == "New device"


def test_exactly_four_characters_is_enough():
    assert default_device_name("48d0") == "Lugo-48D0"
