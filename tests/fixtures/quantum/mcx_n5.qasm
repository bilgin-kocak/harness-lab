OPENQASM 2.0;
include "qelib1.inc";
// A three-controlled X (synthesized by the mcx high-level synthesis plugins).
qreg q[5];
h q[0];
h q[1];
x q[2];
c3x q[0],q[1],q[2],q[3];
cx q[3],q[4];
