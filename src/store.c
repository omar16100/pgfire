#include "pgfire.h"
#include <curl/curl.h>
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

volatile sig_atomic_t pf_stopping;

int pf_fail(pf_store *s, const char *format, ...) {
    va_list args;
    va_start(args, format);
    vsnprintf(s->error, sizeof(s->error), format, args);
    va_end(args);
    return -1;
}

double pf_monotonic(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static int random_id(char out[PF_ID_SIZE]) {
    unsigned char bytes[16];
    if (RAND_bytes(bytes, sizeof(bytes)) != 1) return -1;
    for (size_t i = 0; i < sizeof(bytes); i++) snprintf(out + i * 2, 3, "%02x", bytes[i]);
    return 0;
}

static int valid_name(const char *name) {
    if (!name || !*name || strlen(name) > 96) return 0;
    for (const char *p = name; *p; p++)
        if (!(isalnum((unsigned char)*p) || *p == '-' || *p == '_')) return 0;
    return 1;
}

void pf_string(struct json_object *fields, const char *key, const char *value) {
    struct json_object *v = json_object_new_object();
    json_object_object_add(v, "stringValue", json_object_new_string(value));
    json_object_object_add(fields, key, v);
}

void pf_integer(struct json_object *fields, const char *key, int64_t value) {
    char text[32];
    snprintf(text, sizeof(text), "%" PRId64, value);
    struct json_object *v = json_object_new_object();
    json_object_object_add(v, "integerValue", json_object_new_string(text));
    json_object_object_add(fields, key, v);
}

static struct json_object *member(struct json_object *o, const char *key) {
    struct json_object *v = NULL;
    if (o) json_object_object_get_ex(o, key, &v);
    return v;
}

const char *pf_text(struct json_object *fields, const char *key) {
    const char *s = json_object_get_string(member(member(fields, key), "stringValue"));
    return s ? s : "";
}

int64_t pf_number(struct json_object *fields, const char *key) {
    return json_object_get_int64(member(member(fields, key), "integerValue"));
}

struct json_object *pf_fields(struct json_object *document) { return member(document, "fields"); }

struct response { char *data; size_t length; };

static size_t receive(void *data, size_t size, size_t count, void *context) {
    struct response *r = context;
    if (size && count > SIZE_MAX / size) return 0;
    size_t bytes = size * count;
    if (bytes > 4 * 1024 * 1024 || r->length > 4 * 1024 * 1024 - bytes) return 0;
    char *next = realloc(r->data, r->length + bytes + 1);
    if (!next) return 0;
    r->data = next;
    memcpy(r->data + r->length, data, bytes);
    r->length += bytes;
    r->data[r->length] = 0;
    return bytes;
}

/* Never blindly retry writes: an HTTP timeout has an unknown commit outcome. */
static int request(pf_store *s, const char *url, struct json_object *body,
                   struct json_object **result) {
    *result = NULL;
    s->http_status = 0;
    CURL *curl = s->http;
    if (!curl) return pf_fail(s, "curl initialization failed");
    /* Reset request options, retaining the connection pool for WAL/recovery. */
    curl_easy_reset(curl);
    struct response response = {0};
    struct curl_slist *headers = curl_slist_append(NULL, "Content-Type: application/json");
    const char *token = getenv("PGFIRE_ACCESS_TOKEN");
    char auth[8192];
    if (token && *token) {
        if (strlen(token) > 8000 || strchr(token, '\r') || strchr(token, '\n')) {
            curl_slist_free_all(headers);
            return pf_fail(s, "invalid access token");
        }
        snprintf(auth, sizeof(auth), "Authorization: Bearer %s", token);
        headers = curl_slist_append(headers, auth);
    }
    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, receive);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 5L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    if (body) curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_object_to_json_string_ext(body, JSON_C_TO_STRING_PLAIN));
    CURLcode rc = curl_easy_perform(curl);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &s->http_status);
    curl_slist_free_all(headers);
    if (rc != CURLE_OK) {
        free(response.data);
        return pf_fail(s, "Firestore transport failed (%s); write outcome may be unknown", curl_easy_strerror(rc));
    }
    if (response.data) *result = json_tokener_parse(response.data);
    free(response.data);
    if (!*result) return pf_fail(s, "Firestore returned invalid JSON (HTTP %ld)", s->http_status);
    if (s->http_status < 200 || s->http_status >= 300) {
        const char *status = json_object_get_string(member(member(*result, "error"), "status"));
        pf_fail(s, "Firestore HTTP %ld (%s)", s->http_status, status ? status : "unknown");
        json_object_put(*result); *result = NULL;
        return -1;
    }
    return 0;
}

int pf_open(pf_store *s) {
    memset(s, 0, sizeof(*s));
    const char *endpoint = getenv("FIRESTORE_EMULATOR_HOST");
    const char *project = getenv("PGFIRE_PROJECT");
    const char *space = getenv("PGFIRE_NAMESPACE");
    const char *database = getenv("PGFIRE_DATABASE");
    if (!project) project = "pgfire-local";
    if (!space) space = "development";
    if (!database) database = "(default)";
    if (!valid_name(project) || !valid_name(space) ||
        (strcmp(database, "(default)") && !valid_name(database)))
        return pf_fail(s, "invalid project, database, or namespace");
    if (endpoint) {
        if (strlen(endpoint) > 450 || strpbrk(endpoint, "/?#@\r\n"))
            return pf_fail(s, "FIRESTORE_EMULATOR_HOST must be host:port");
        snprintf(s->endpoint, sizeof(s->endpoint), "http://%s", endpoint);
    } else {
        if (!getenv("PGFIRE_ACCESS_TOKEN")) return pf_fail(s, "set FIRESTORE_EMULATOR_HOST or PGFIRE_ACCESS_TOKEN");
        snprintf(s->endpoint, sizeof(s->endpoint), "https://firestore.googleapis.com");
    }
    snprintf(s->root, sizeof(s->root), "projects/%s/databases/%s/documents/pgfire/%s", project, database, space);
    if (random_id(s->owner)) return pf_fail(s, "secure randomness unavailable");
    const char *owner = getenv("PGFIRE_OWNER_TOKEN");
    if (owner) {
        if (strlen(owner) != 32 || strspn(owner, "0123456789abcdef") != 32)
            return pf_fail(s, "invalid owner transfer token");
        memcpy(s->owner, owner, 33);
    }
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != 0) return pf_fail(s, "curl global initialization failed");
    s->http = curl_easy_init();
    if (!s->http) { curl_global_cleanup(); return pf_fail(s, "curl initialization failed"); }
    return 0;
}

void pf_close(pf_store *s) {
    json_object_put(s->state);
    s->state = NULL;
    curl_easy_cleanup(s->http);
    s->http = NULL;
    curl_global_cleanup();
}

int pf_get(pf_store *s, const char *path, struct json_object **result) {
    if (s->held && pf_monotonic() > s->deadline - 30 && pf_renew(s)) return -1;
    char url[2048];
    if (snprintf(url, sizeof(url), "%s/v1/%s/%s", s->endpoint, s->root, path) >= (int)sizeof(url))
        return pf_fail(s, "document URL too long");
    return request(s, url, NULL, result);
}

static struct json_object *write_doc(pf_store *s, const char *path, struct json_object *fields,
                                     const char *version) {
    char name[1400];
    snprintf(name, sizeof(name), "%s/%s", s->root, path);
    struct json_object *write = json_object_new_object();
    struct json_object *doc = json_object_new_object();
    struct json_object *pre = json_object_new_object();
    json_object_object_add(doc, "name", json_object_new_string(name));
    json_object_object_add(doc, "fields", json_object_get(fields));
    json_object_object_add(write, "update", doc);
    if (version && *version) json_object_object_add(pre, "updateTime", json_object_new_string(version));
    else json_object_object_add(pre, "exists", json_object_new_boolean(0));
    json_object_object_add(write, "currentDocument", pre);
    return write;
}

static int commit(pf_store *s, struct json_object *writes, struct json_object **result) {
    char url[1800], database[1024];
    snprintf(database, sizeof(database), "%s", s->root);
    char *separator = strstr(database, "/documents/");
    if (!separator) return pf_fail(s, "invalid database root");
    *separator = 0;
    snprintf(url, sizeof(url), "%s/v1/%s/documents:commit", s->endpoint, database);
    struct json_object *body = json_object_new_object();
    json_object_object_add(body, "writes", json_object_get(writes));
    int rc = request(s, url, body, result);
    json_object_put(body);
    return rc;
}

int pf_load(pf_store *s) {
    struct json_object *doc = NULL;
    if (pf_get(s, "meta/state", &doc)) {
        if (s->http_status != 404) return -1;
        json_object_put(s->state);
        s->state = json_object_new_object();
        s->version[0] = 0;
        s->error[0] = 0;
        return 0;
    }
    struct json_object *fields = pf_fields(doc);
    const char *version = json_object_get_string(member(doc, "updateTime"));
    if (!fields || !version || strlen(version) >= sizeof(s->version)) {
        json_object_put(doc); return pf_fail(s, "malformed state document");
    }
    json_object_put(s->state);
    s->state = json_object_get(fields);
    snprintf(s->version, sizeof(s->version), "%s", version);
    json_object_put(doc);
    return 0;
}

static int state_commit(pf_store *s, const char *record_id, struct json_object *record) {
    double began = pf_monotonic();
    if (s->held && began >= s->deadline) {
        s->held = 0;
        return pf_fail(s, "lease deadline exceeded; refusing publication");
    }
    pf_string(s->state, "owner", s->owner);
    pf_integer(s->state, "expires", (int64_t)time(NULL) + PF_LEASE_SECONDS);
    struct json_object *writes = json_object_new_array();
    json_object_array_add(writes, write_doc(s, "meta/state", s->state, s->version));
    if (record) {
        char path[128];
        snprintf(path, sizeof(path), "wal/%s", record_id);
        json_object_array_add(writes, write_doc(s, path, record, NULL));
    }
    struct json_object *response = NULL;
    int rc = commit(s, writes, &response);
    json_object_put(writes);
    if (rc) {
        /* Exit without acknowledging: recovery resolves the durable prefix. */
        s->held = 0;
        return -1;
    }
    struct json_object *results = member(response, "writeResults");
    const char *version = json_object_get_string(member(json_object_array_get_idx(results, 0), "updateTime"));
    if (!version || strlen(version) >= sizeof(s->version)) {
        json_object_put(response); s->held = 0;
        return pf_fail(s, "missing commit version; refusing acknowledgement");
    }
    snprintf(s->version, sizeof(s->version), "%s", version);
    json_object_put(response);
    s->deadline = began + PF_LEASE_SECONDS - 10;
    s->held = 1;
    /* The independent supervisor must also stop reads if this process stalls. */
    const char *lease_file = getenv("PGFIRE_LEASE_FILE");
    if (lease_file && *lease_file) {
        char temporary[2048];
        if (snprintf(temporary, sizeof(temporary), "%s.tmp.%ld", lease_file, (long)getpid()) >= (int)sizeof(temporary))
            return pf_fail(s, "lease watchdog path too long");
        FILE *out = fopen(temporary, "w");
        if (!out) return pf_fail(s, "cannot create lease watchdog file");
        int failed = fprintf(out, "%.9f\n", s->deadline) < 0;
        if (fclose(out)) failed = 1;
        if (failed || rename(temporary, lease_file)) {
            remove(temporary);
            return pf_fail(s, "cannot publish lease watchdog deadline");
        }
    }
    return 0;
}

int pf_claim(pf_store *s) {
    if (pf_load(s)) return -1;
    if (*pf_text(s->state, "owner") && pf_number(s->state, "expires") > (int64_t)time(NULL)) {
        if (strcmp(pf_text(s->state, "owner"), s->owner))
            return pf_fail(s, "database already has an unexpired owner");
        s->deadline = pf_monotonic() + (double)(pf_number(s->state, "expires") - (int64_t)time(NULL)) - 10;
        s->held = 1;
        return pf_renew(s);
    }
    pf_integer(s->state, "epoch", pf_number(s->state, "epoch") + 1);
    return state_commit(s, NULL, NULL);
}

int pf_renew(pf_store *s) {
    if (!s->held) return pf_fail(s, "lease is not held");
    return state_commit(s, NULL, NULL);
}

int pf_release(pf_store *s) {
    if (!s->held) return 0;
    pf_string(s->state, "owner", "");
    pf_integer(s->state, "expires", 0);
    struct json_object *writes = json_object_new_array();
    json_object_array_add(writes, write_doc(s, "meta/state", s->state, s->version));
    struct json_object *response = NULL;
    int rc = commit(s, writes, &response);
    json_object_put(writes); json_object_put(response);
    s->held = 0;
    return rc;
}

int pf_publish(pf_store *s, const char *record_id, struct json_object *record) {
    if (!s->held) return pf_fail(s, "lease is not held");
    if (record) {
        int64_t count = pf_number(s->state, "wal_records");
        if (count < 0 || count >= PF_MAX_WAL_RECORDS)
            return pf_fail(s, "WAL retention limit reached; offline checkpoint required");
        pf_integer(s->state, "wal_records", count + 1);
    }
    return state_commit(s, record_id, record);
}

int pf_put_immutable(pf_store *s, const char *path, struct json_object *fields) {
    if (!s->held) return pf_fail(s, "lease is not held");
    if (pf_monotonic() > s->deadline - 30 && pf_renew(s)) return -1;
    struct json_object *writes = json_object_new_array();
    json_object_array_add(writes, write_doc(s, path, fields, NULL));
    struct json_object *response = NULL;
    int rc = commit(s, writes, &response);
    json_object_put(writes); json_object_put(response);
    return rc;
}

static void hex_digest(const unsigned char digest[32], char output[65]) {
    for (size_t i = 0; i < 32; i++) snprintf(output + i * 2, 3, "%02x", digest[i]);
}

static int upload_chunk(pf_store *s, const char *id, size_t index, const unsigned char *data, size_t length) {
    if (length > PF_CHUNK) return pf_fail(s, "chunk exceeds bound");
    size_t encoded_size = 4 * ((length + 2) / 3) + 1;
    unsigned char *encoded = malloc(encoded_size);
    if (!encoded) return pf_fail(s, "out of memory");
    int n = EVP_EncodeBlock(encoded, data, (int)length);
    struct json_object *fields = json_object_new_object();
    struct json_object *bytes = json_object_new_object();
    json_object_object_add(bytes, "bytesValue", json_object_new_string_len((const char *)encoded, n));
    json_object_object_add(fields, "data", bytes);
    char path[128];
    snprintf(path, sizeof(path), "objects/%s/chunks/%08zu", id, index);
    int rc = pf_put_immutable(s, path, fields);
    json_object_put(fields); free(encoded);
    return rc;
}

static int finish_upload(pf_store *s, const char *id, size_t length, size_t chunks, EVP_MD_CTX *digest) {
    unsigned char hash[EVP_MAX_MD_SIZE];
    unsigned int hash_length;
    if (EVP_DigestFinal_ex(digest, hash, &hash_length) != 1 || hash_length != 32)
        return pf_fail(s, "SHA-256 failed");
    char hex[65], path[128];
    hex_digest(hash, hex);
    struct json_object *fields = json_object_new_object();
    pf_integer(fields, "size", (int64_t)length);
    pf_integer(fields, "chunks", (int64_t)chunks);
    pf_string(fields, "sha256", hex);
    snprintf(path, sizeof(path), "objects/%s", id);
    int rc = pf_put_immutable(s, path, fields);
    json_object_put(fields);
    return rc;
}

int pf_upload(pf_store *s, const void *data, size_t size, char id[PF_ID_SIZE]) {
    if (random_id(id)) return pf_fail(s, "secure randomness unavailable");
    EVP_MD_CTX *digest = EVP_MD_CTX_new();
    if (!digest || EVP_DigestInit_ex(digest, EVP_sha256(), NULL) != 1) {
        EVP_MD_CTX_free(digest); return pf_fail(s, "SHA-256 initialization failed");
    }
    size_t chunks = 0;
    int rc = 0;
    for (size_t offset = 0; offset < size && !rc; chunks++) {
        size_t length = size - offset;
        if (length > PF_CHUNK) length = PF_CHUNK;
        if (pf_stopping || EVP_DigestUpdate(digest, (const unsigned char *)data + offset, length) != 1)
            rc = pf_fail(s, "upload interrupted");
        else rc = upload_chunk(s, id, chunks, (const unsigned char *)data + offset, length);
        offset += length;
    }
    if (!rc) rc = finish_upload(s, id, size, chunks, digest);
    EVP_MD_CTX_free(digest);
    return rc;
}

int pf_upload_file(pf_store *s, const char *file, char id[PF_ID_SIZE]) {
    FILE *input = fopen(file, "rb");
    if (!input) return pf_fail(s, "cannot open input: %s", strerror(errno));
    unsigned char *buffer = malloc(PF_CHUNK);
    EVP_MD_CTX *digest = EVP_MD_CTX_new();
    if (!buffer || !digest || random_id(id) || EVP_DigestInit_ex(digest, EVP_sha256(), NULL) != 1) {
        free(buffer); EVP_MD_CTX_free(digest); fclose(input);
        return pf_fail(s, "upload initialization failed");
    }
    size_t chunks = 0, total = 0, n;
    int rc = 0;
    while (!rc && (n = fread(buffer, 1, PF_CHUNK, input)) > 0) {
        if (pf_stopping || EVP_DigestUpdate(digest, buffer, n) != 1) rc = pf_fail(s, "upload interrupted");
        else rc = upload_chunk(s, id, chunks++, buffer, n);
        total += n;
    }
    if (ferror(input)) rc = pf_fail(s, "input read error");
    if (!rc) rc = finish_upload(s, id, total, chunks, digest);
    free(buffer); EVP_MD_CTX_free(digest); fclose(input);
    return rc;
}

static int download_object(pf_store *s, const char *id, FILE *output, unsigned char **memory, size_t *memory_size) {
    if (!valid_name(id)) return pf_fail(s, "invalid object id");
    char path[128];
    snprintf(path, sizeof(path), "objects/%s", id);
    struct json_object *doc = NULL;
    if (pf_get(s, path, &doc)) return -1;
    int64_t length = pf_number(pf_fields(doc), "size");
    int64_t chunks = pf_number(pf_fields(doc), "chunks");
    char expected[65];
    snprintf(expected, sizeof(expected), "%s", pf_text(pf_fields(doc), "sha256"));
    json_object_put(doc);
    if (length < 0 || length > INT64_MAX - PF_CHUNK || chunks < 0 ||
        chunks != (length + PF_CHUNK - 1) / PF_CHUNK || strlen(expected) != 64)
        return pf_fail(s, "invalid object manifest");
    if (memory && length > 64 * 1024 * 1024) return pf_fail(s, "object exceeds memory bound");
    unsigned char *data = memory ? malloc((size_t)length + 1) : NULL;
    if (memory && !data) return pf_fail(s, "out of memory");
    EVP_MD_CTX *digest = EVP_MD_CTX_new();
    if (!digest || EVP_DigestInit_ex(digest, EVP_sha256(), NULL) != 1) {
        free(data); EVP_MD_CTX_free(digest); return pf_fail(s, "SHA-256 initialization failed");
    }
    size_t total = 0;
    int rc = 0;
    for (int64_t i = 0; i < chunks && !rc; i++) {
        snprintf(path, sizeof(path), "objects/%s/chunks/%08" PRId64, id, i);
        if (pf_get(s, path, &doc)) { rc = -1; break; }
        const char *encoded = json_object_get_string(member(member(pf_fields(doc), "data"), "bytesValue"));
        size_t encoded_length = encoded ? strlen(encoded) : 0;
        unsigned char *decoded = malloc(encoded_length + 1);
        if (!decoded || !encoded_length || encoded_length > 4 * ((PF_CHUNK + 2) / 3) || encoded_length % 4) {
            free(decoded); json_object_put(doc); rc = pf_fail(s, "malformed chunk"); break;
        }
        int n = EVP_DecodeBlock(decoded, (const unsigned char *)encoded, (int)encoded_length);
        if (n >= 0 && encoded[encoded_length - 1] == '=') n--;
        if (n >= 0 && encoded[encoded_length - 2] == '=') n--;
        size_t wanted = (size_t)length - total;
        if (wanted > PF_CHUNK) wanted = PF_CHUNK;
        if (n < 0 || (size_t)n != wanted || EVP_DigestUpdate(digest, decoded, wanted) != 1)
            rc = pf_fail(s, "corrupt chunk");
        else if (output && fwrite(decoded, 1, wanted, output) != wanted) rc = pf_fail(s, "output write failed");
        else if (data) memcpy(data + total, decoded, wanted);
        total += wanted;
        free(decoded); json_object_put(doc);
    }
    unsigned char hash[EVP_MAX_MD_SIZE]; unsigned int hash_length;
    char actual[65];
    if (!rc && EVP_DigestFinal_ex(digest, hash, &hash_length) == 1 && hash_length == 32) {
        hex_digest(hash, actual);
        if (strcmp(actual, expected) || total != (size_t)length) rc = pf_fail(s, "object checksum mismatch");
    } else if (!rc) rc = pf_fail(s, "checksum calculation failed");
    EVP_MD_CTX_free(digest);
    if (rc) free(data);
    else if (memory) { *memory = data; *memory_size = total; }
    return rc;
}

int pf_download(pf_store *s, const char *id, const char *file) {
    FILE *output = fopen(file, "wbx");
    if (!output) return pf_fail(s, "cannot create output: %s", strerror(errno));
    int rc = download_object(s, id, output, NULL, NULL);
    if (fclose(output) && !rc) rc = pf_fail(s, "output close failed");
    if (rc) remove(file);
    return rc;
}

int pf_read_object(pf_store *s, const char *id, unsigned char **data, size_t *size) {
    return download_object(s, id, NULL, data, size);
}

uint64_t pf_parse_lsn(const char *text, int *ok) {
    *ok = 0;
    if (!text || !isxdigit((unsigned char)*text)) return 0;
    char *end;
    errno = 0;
    unsigned long high = strtoul(text, &end, 16);
    if (errno || end == text || *end != '/' || high > UINT32_MAX) return 0;
    const char *low_text = end + 1;
    if (!isxdigit((unsigned char)*low_text)) return 0;
    unsigned long low = strtoul(low_text, &end, 16);
    if (errno || *end || low > UINT32_MAX) return 0;
    *ok = 1;
    return ((uint64_t)high << 32) | low;
}

void pf_format_lsn(uint64_t lsn, char out[32]) {
    snprintf(out, 32, "%X/%X", (unsigned int)(lsn >> 32), (unsigned int)(lsn & UINT32_MAX));
}

uint64_t pf_get_u64(const unsigned char *data) {
    uint64_t value = 0;
    for (size_t i = 0; i < 8; i++) value = (value << 8) | data[i];
    return value;
}

void pf_set_u64(unsigned char *data, uint64_t value) {
    for (int i = 7; i >= 0; i--) { data[i] = (unsigned char)(value & 255); value >>= 8; }
}
