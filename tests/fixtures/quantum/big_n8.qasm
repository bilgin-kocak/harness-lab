OPENQASM 2.0;
include "qelib1.inc";
// Eight qubits: more than the seven-qubit test target, so compilation fails.
qreg q[8];
h q[0];
cx q[0],q[7];
cx q[7],q[3];
