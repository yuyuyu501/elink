#include <assert.h>
#include <stdio.h>
#include "protocol.h"
int main(void) {
    EP_STATE s = {0};
    EP_INPUT input = {{EP_VERSION, sizeof(EP_INPUT)}, 1, {0x1000, 255, 128, -32768, 32767, -1, 1}};
    uint8_t wire[29], rumble[5] = {0, 0, 128, 255, 2};
    size_t n;
    assert(sizeof(EP_PAD) == 12 && sizeof(EP_INPUT) == 28 && sizeof(EP_FEEDBACK) == 16);
    assert(!ep_claim(&s, 0)); assert(ep_claim(&s, 11)); assert(!ep_claim(&s, 12));
    assert(!ep_update(&s, 12, &input, sizeof(input), 100));
    for (n = 0; n < sizeof(input); n++) assert(!ep_update(&s, 11, &input, n, 100));
    input.header.version = 2; assert(!ep_update(&s, 11, &input, sizeof(input), 100)); input.header.version = 1;
    assert(ep_update(&s, 11, &input, sizeof(input), 100)); assert(s.packet == 1);
    assert(!ep_update(&s, 11, &input, sizeof(input), 101));
    input.sequence++; assert(ep_update(&s, 11, &input, sizeof(input), 102)); assert(s.packet == 1);
    ep_xusb_state(&s, wire, 0);
    assert(wire[0] == 3 && wire[1] == 1 && wire[2] == 1 && wire[5] == 1);
    assert(wire[11] == 0 && wire[12] == 0x10 && wire[13] == 255 && wire[14] == 128);
    assert(wire[15] == 0 && wire[16] == 128 && wire[17] == 255 && wire[18] == 127);
    ep_xusb_state(&s, wire, 1); assert(wire[2] == 3 && wire[10] == 0x14);
    assert(ep_rumble(&s, rumble, 5)); assert(s.feedback.left == 32896 && s.feedback.right == 65535);
    rumble[4] = 1; rumble[2] = 0; assert(ep_rumble(&s, rumble, 5)); assert(s.feedback.left == 32896);
    rumble[4] = 4; assert(!ep_rumble(&s, rumble, 5)); assert(!ep_rumble(&s, rumble, 4));
    ep_expire(&s, 601); assert(s.pad.buttons == 0x1000);
    ep_expire(&s, 602); assert(s.pad.buttons == 0 && s.feedback.left == 0 && s.packet == 2);
    ep_release(&s, 12); assert(s.owner == 11);
    input.sequence++; input.pad.buttons = 0x0800; assert(!ep_update(&s, 11, &input, sizeof(input), 603));
    input.pad.buttons = 0x2000; assert(ep_update(&s, 11, &input, sizeof(input), 603));
    ep_release(&s, 11); assert(s.owner == 0 && s.pad.buttons == 0);
    assert(ep_claim(&s, 12)); input.sequence = 1; assert(ep_update(&s, 12, &input, sizeof(input), 700));
    puts("ElinkPad: ABI, ownership, input validation, ordering, wire state, rumble, expiry and release passed.");
    return 0;
}
