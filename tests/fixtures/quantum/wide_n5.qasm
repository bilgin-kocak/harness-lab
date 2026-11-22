OPENQASM 2.0;
include "qelib1.inc";
// A pool-B stand-in: checked by QCEC alone in the tests.
qreg q[5];
h q[0];
cx q[0],q[4];
ccx q[4],q[1],q[2];
rz(0.25) q[2];
cx q[2],q[3];
sx q[1];
