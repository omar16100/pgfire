#include "pgfire.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    const uint64_t samples[] = {0, 1, UINT32_MAX, UINT64_C(0x0123456789abcdef), UINT64_MAX};
    for (size_t i = 0; i < sizeof(samples) / sizeof(samples[0]); i++) {
        unsigned char bytes[8]; char text[32]; int ok = 0;
        pf_set_u64(bytes, samples[i]);
        assert(pf_get_u64(bytes) == samples[i]);
        pf_format_lsn(samples[i], text);
        assert(pf_parse_lsn(text, &ok) == samples[i] && ok);
    }
    const unsigned char wire[] = {0, 0, 0, 1, 0, 0, 0, 2};
    assert(pf_get_u64(wire) == UINT64_C(0x100000002));
    const char *bad[] = {"", "/", "0/", "/0", "-1/0", " 1/0", "1/ 0", "1/2garbage", "100000000/0", "0/100000000"};
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        int ok = 1; (void)pf_parse_lsn(bad[i], &ok); assert(!ok);
    }
    struct json_object *fields = json_object_new_object();
    pf_integer(fields, "epoch", INT64_MAX);
    pf_string(fields, "object", "test");
    assert(pf_number(fields, "epoch") == INT64_MAX);
    assert(!strcmp(pf_text(fields, "object"), "test"));
    assert(!strcmp(pf_text(fields, "missing"), ""));
    json_object_put(fields);
    puts("codec and Firestore integer precision tests passed");
    return 0;
}
