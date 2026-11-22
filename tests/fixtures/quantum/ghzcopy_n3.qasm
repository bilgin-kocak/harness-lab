OPENQASM 2.0;
include "qelib1.inc";
// The same circuit as ghz_n3 without the final measurements: identical once they are removed.
qreg q[3];
h q[0];
cx q[0],q[1];
cx q[1],q[2];
t q[2];
