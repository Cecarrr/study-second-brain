# Process Scheduling

A process moves between states: new, ready, running, waiting, and terminated.
The scheduler decides which ready process gets the CPU next.

## FCFS (First Come First Served)
Processes run in arrival order. Simple, but it suffers from the convoy effect: short processes wait behind one long process, so average waiting time grows.

## SJF (Shortest Job First)
Runs the process with the shortest burst time first. It gives the lowest average waiting time, but long processes may suffer starvation.

## Round Robin
Each process gets a fixed time quantum, then goes to the back of the queue. Good for time-sharing systems. If the quantum is too large it behaves like FCFS; if too small, context switching overhead dominates.

## Priority Scheduling
The highest priority runs first. Low-priority processes may starve. The common fix is aging: gradually increasing the priority of processes that wait for a long time.
