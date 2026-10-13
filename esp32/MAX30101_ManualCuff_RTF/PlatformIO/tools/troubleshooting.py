"""Explain failed checks without claiming a physical diagnosis from PPG alone."""


def suggestions(reason, results=()):
    if not reason:
        return []
    text=reason.lower()
    if results:
        best=max(results,key=lambda r:r['score'])
        text+=' '+' '.join(best.get('feedback',[])).lower()
    tips=[]
    def add(tip):
        if tip not in tips:tips.append(tip)
    if any(key in text for key in ('byte','usb','i2c','fifo','sample','disconnect','connection','firmware','configuration','acknowledg','sensor_init','winerror')):
        add('Acquisition or communication failed. With the cuff released, check sensor power/ground, connectors and the USB data cable; try a direct USB port. Reconnect before recalibrating.')
        if any(key in text for key in ('wifi','wi-fi','network','disconnect','winerror')):
            add('For Wi-Fi, check that the recorder and computer remain on the same network; compare with USB to isolate the connection.')
        add('Missing or corrupt samples cannot establish pulse suppression. This error alone does not identify whether the cause is power, wiring, firmware or transport.')
    if any(key in text for key in ('clipp','brightness','light level','led limit','flatline','stuck adc','contact lost','fewer than two')):
        add('The optical level is too low, clipped or flat. With the cuff deflated, check sensor orientation, full contact and light shielding. Use steady, gentle contact rather than tightening the sensor to force a signal.')
    if any(key in text for key in ('drift','jump','movement','baseline changed','optical level change')):
        add('The optical level is changing too much. Support the arm, secure cable strain and shield ambient light; check for changing sensor contact or pressure. Compare a cuff-deflated recording at fixed LED settings before retrying.')
        add('DC changes can also reflect blood-volume changes. The recorded signal cannot reliably distinguish these from contact changes; the larger GREEN limit does not repair the underlying drift.')
    if any(key in text for key in ('pulse too small','pulse fit weak','repeat reliably','agreement too low','timing differs','signal strength','varied too much','no pair')):
        add('The pulse was too weak or inconsistent across windows/channels. Keep the cuff deflated, let the signal settle, check placement/contact and shielding, then retry calibration. The failed attempt can still guide the next current search.')
        add('If placement or contact changed substantially, remove the earlier calibration attempt or turn off history before retrying; old settings may no longer be useful.')
    if any(key in text for key in ('suppression','coherent pulse remains','90-second','cuff_90')):
        add('Suppression was not confirmed: residual pulsation, drift or noise may still exceed the gates. Release the cuff and review the per-channel pulse/RMS ratios. Do not increase pressure or duration just to obtain a pass.')
    if not tips:
        add('Keep the cuff fully deflated, review the last failed check and verify a stable optical signal before a fresh calibration. A failed check is not a diagnosis of its physical cause.')
    return tips
