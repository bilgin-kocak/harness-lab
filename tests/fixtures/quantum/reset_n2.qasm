OPENQASM 2.0;
include "qelib1.inc";
// A reset in the middle of the circuit.
qreg q[2];
h q[0];
reset q[0];
cx q[0],q[1];
