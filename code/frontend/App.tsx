import React from 'react';
import { MemoryRouter, Routes, Route, Navigate } from 'react-router-dom';
import PatientList from './components/PatientList/PatientList';
import Dashboard from './components/Dashboard';

const App: React.FC = () => {
  return (
    <MemoryRouter>
      <Routes>
        <Route path="/" element={<PatientList />} />
        <Route path="/dashboard/:patientId" element={<Dashboard />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </MemoryRouter>
  );
};

export default App;