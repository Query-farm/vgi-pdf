# Copyright 2026 Query Farm LLC - https://query.farm
#
# Rewrite each `require <ext>` gate (and the bare `LOAD vgi;` these tests use
# instead — haybarn skips `require vgi`, so the .test files LOAD it directly)
# in this repo's sqllogictest files into an explicit signed INSTALL+LOAD, so the
# prebuilt standalone `haybarn-unittest` (which links none of these extensions)
# can run the suite. The vgi extension comes from the signed community channel;
# httpfs/json/parquet/spatial from the signed core channel. `require-env` and
# every other directive pass through untouched. See ci/README.md.
/^require[ \t]+vgi[ \t]*$/ {
    print "statement ok"; print "INSTALL vgi FROM community;"; print "";
    print "statement ok"; print "LOAD vgi;"; next
}
/^require[ \t]+(httpfs|json|parquet|spatial)[ \t]*$/ {
    ext = $2
    print "statement ok"; print "INSTALL " ext " FROM core;"; print "";
    print "statement ok"; print "LOAD " ext ";"; next
}
# These tests gate the worker with `require-env` and then `LOAD vgi;` directly
# (haybarn silently SKIPs `require vgi`). Inject a signed community INSTALL right
# before that bare LOAD so a clean runner can resolve the extension.
/^[ \t]*LOAD[ \t]+vgi[ \t]*;[ \t]*$/ {
    print "INSTALL vgi FROM community;"; print $0; next
}
{ print }
